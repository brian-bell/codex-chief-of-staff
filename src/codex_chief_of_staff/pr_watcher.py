"""Deterministic classification and bounded retry policy for GitHub PRs."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .github_pr import ProviderError, validate_repository


CLASSIFICATIONS = frozenset(
    {
        "checks-pending", "deterministic-check-failure",
        "plausible-transient-failure", "blocking-review-feedback",
        "conflict", "product-gate", "stale-head", "merge-ready",
    }
)
TRANSIENT_CHECK_CONCLUSIONS = frozenset(
    {"CANCELLED", "STALE", "STARTUP_FAILURE", "TIMED_OUT"}
)
FAILED_CHECK_CONCLUSIONS = frozenset(
    {"ACTION_REQUIRED", "FAILURE"}
)
SUCCESSFUL_CHECK_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
VALID_REVIEW_DECISIONS = frozenset(
    {None, "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}
)
VALID_REVIEW_STATES = frozenset(
    {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"}
)
VALID_CHECK_STATUSES = frozenset(
    {"COMPLETED", "IN_PROGRESS", "PENDING", "QUEUED", "REQUESTED", "WAITING"}
)
VALID_CHECK_CONCLUSIONS = frozenset(
    {
        None, "ACTION_REQUIRED", "CANCELLED", "FAILURE", "NEUTRAL",
        "SKIPPED", "STALE", "STARTUP_FAILURE", "SUCCESS", "TIMED_OUT",
    }
)
VALID_STATUS_STATES = frozenset({"ERROR", "EXPECTED", "FAILURE", "PENDING", "SUCCESS"})
VALID_MERGEABLE = frozenset({"MERGEABLE", "CONFLICTING", "UNKNOWN"})
VALID_MERGE_STATES = frozenset(
    {"BEHIND", "BLOCKED", "CLEAN", "DIRTY", "DRAFT", "HAS_HOOKS", "UNKNOWN", "UNSTABLE"}
)
PRODUCT_MERGE_STATES = frozenset(
    {"BEHIND", "BLOCKED", "DRAFT", "HAS_HOOKS", "UNKNOWN", "UNSTABLE"}
)
MAX_CONNECTION_NODES = 100
MAX_PROVIDER_ERRORS = 20
MAX_NAME_LENGTH = 256
MAX_LOGIN_LENGTH = 100
MAX_URL_LENGTH = 2048
MAX_OID_LENGTH = 64
MAX_TIMESTAMP_LENGTH = 64
MAX_THREAD_ID_LENGTH = 256
SAFE_OID = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    delays: tuple[float, ...] = (1.0, 5.0)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if len(self.delays) < self.max_attempts - 1:
            raise ValueError("retry policy needs one delay per retry")
        if any(delay < 0 for delay in self.delays):
            raise ValueError("retry delays cannot be negative")

    def delay_after(self, attempt: int) -> float:
        return self.delays[attempt - 1]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_timestamp(value: object) -> str:
    errors: list[str] = []
    normalized = _timestamp(value, "timestamp", errors, required=True)
    if errors or normalized is None:
        raise ValueError("timestamp must be a timezone-aware ISO-8601 instant")
    return normalized


def validate_oid(value: object, field: str = "head SHA") -> str:
    if (
        not isinstance(value, str) or not value or len(value) > MAX_OID_LENGTH
        or SAFE_OID.fullmatch(value) is None
    ):
        raise ValueError(f"{field} must be a bounded SHA-like identifier")
    return value


def _as_dict(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _safe_text(
    value: object, path: str, errors: list[str], *, limit: int,
    required: bool = True,
) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str) or not value or len(value) > limit
        or any(ord(character) < 32 for character in value)
    ):
        errors.append(f"{path} is missing, invalid, or exceeds its limit")
        return None
    return value


def _oid(value: object, path: str, errors: list[str]) -> str | None:
    text = _safe_text(value, path, errors, limit=MAX_OID_LENGTH)
    if text is not None and SAFE_OID.fullmatch(text) is None:
        errors.append(f"{path} is not a safe object identifier")
        return None
    return text


def _timestamp(
    value: object, path: str, errors: list[str], *, required: bool,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_TIMESTAMP_LENGTH:
        errors.append(f"{path} timestamp is missing, invalid, or exceeds its limit")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{path} timestamp is not a valid ISO-8601 instant")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{path} timestamp must include a timezone")
        return None
    return (
        parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _connection_nodes(
    value: object, path: str, errors: list[str], *,
    limit: int = MAX_CONNECTION_NODES, require_page_info: bool = True,
) -> list[object]:
    connection = _as_dict(value)
    if connection is None or not isinstance(connection.get("nodes"), list):
        errors.append(f"{path}.nodes is missing or invalid")
        return []
    nodes = connection["nodes"]
    if len(nodes) > limit:
        errors.append(f"{path}.nodes exceeds the {limit}-node limit")
        nodes = nodes[:limit]
    if require_page_info:
        page_info = _as_dict(connection.get("pageInfo"))
        if page_info is None or type(page_info.get("hasNextPage")) is not bool:
            errors.append(f"{path}.pageInfo.hasNextPage is missing or invalid")
        elif page_info["hasNextPage"]:
            errors.append(f"{path} is truncated at {limit} nodes")
    return nodes


def _provider_errors(payload: dict[str, Any]) -> list[dict[str, object]]:
    errors = payload.get("errors")
    if errors is None:
        return []
    if not isinstance(errors, list):
        return [{"kind": "response", "code": "invalid-errors", "retryable": False}]
    normalized: set[tuple[str, str, bool]] = set()
    if len(errors) > MAX_PROVIDER_ERRORS:
        normalized.add(("response", "too-many-errors", False))
    for raw in errors[:MAX_PROVIDER_ERRORS]:
        item = _as_dict(raw)
        if item is None:
            normalized.add(("response", "invalid-error", False))
        elif item.get("type") == "RATE_LIMITED":
            normalized.add(("rate-limit", "rate-limited", True))
        elif item.get("type") == "FORBIDDEN":
            normalized.add(("permission", "forbidden", False))
        else:
            normalized.add(("graphql", "other", False))
    priority = {"permission": 0, "response": 1, "graphql": 2, "rate-limit": 3}
    return [
        {"kind": kind, "code": code, "retryable": retryable}
        for kind, code, retryable in sorted(
            normalized, key=lambda item: (priority[item[0]], item[1], item[2])
        )
    ]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _select_latest(
    candidates: list[dict[str, object]], *, identity_fields: tuple[str, ...],
    timestamp_field: str, path: str, errors: list[str],
    sequence_field: str | None = None,
) -> list[dict[str, object]]:
    groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for candidate in candidates:
        identity = tuple(candidate[field] for field in identity_fields)
        groups.setdefault(identity, []).append(candidate)
    selected = []
    for identity in sorted(groups, key=lambda key: tuple(str(item) for item in key)):
        group = groups[identity]
        sequence_values = (
            [item.get(sequence_field) for item in group] if sequence_field else []
        )
        if sequence_values and any(value is not None for value in sequence_values):
            if any(value is None for value in sequence_values):
                errors.append(f"{path} has incomplete attempt sequence identifiers")
            latest_sequence = max(
                int(value) for value in sequence_values if value is not None
            )
            latest = [
                item for item in group if item.get(sequence_field) == latest_sequence
            ]
        else:
            latest_time = max(str(item[timestamp_field] or "") for item in group)
            latest = [
                item for item in group
                if str(item[timestamp_field] or "") == latest_time
            ]
        variants = {_canonical(item): item for item in latest}
        if len(variants) > 1:
            errors.append(f"{path} has ambiguous attempts with the same timestamp")
        selected.append(variants[sorted(variants)[0]])
    return sorted(selected, key=_canonical)


def _normalize_checks(
    pr: dict[str, Any], observed_head: str | None, errors: list[str]
) -> tuple[list[dict[str, object]], str | None]:
    commit_nodes = _connection_nodes(
        pr.get("commits"), "pullRequest.commits", errors,
        limit=1, require_page_info=False,
    )
    if len(commit_nodes) != 1 or not isinstance(commit_nodes[0], dict):
        errors.append("pullRequest.commits must contain exactly one observed head commit")
        return [], None
    commit = _as_dict(commit_nodes[0].get("commit"))
    if commit is None:
        errors.append("pullRequest.commits head commit is missing or invalid")
        return [], None
    commit_oid = _oid(commit.get("oid"), "pullRequest.commits head oid", errors)
    if commit_oid is not None and observed_head is not None and commit_oid != observed_head:
        errors.append("pullRequest.commits evidence is not bound to the observed head")
    if "statusCheckRollup" not in commit:
        errors.append("pullRequest.commits head statusCheckRollup is missing")
        return [], None
    rollup = commit.get("statusCheckRollup")
    if rollup is None:
        return [], None
    rollup_dict = _as_dict(rollup)
    if rollup_dict is None:
        errors.append("statusCheckRollup is invalid")
        return [], None
    rollup_state = rollup_dict.get("state")
    if rollup_state not in VALID_STATUS_STATES:
        errors.append("statusCheckRollup.state is missing or unknown")
        rollup_state = None
    nodes = _connection_nodes(
        rollup_dict.get("contexts"), "statusCheckRollup.contexts", errors
    )
    candidates: list[dict[str, object]] = []
    for index, raw in enumerate(nodes):
        item = _as_dict(raw)
        if item is None:
            errors.append(f"statusCheckRollup.contexts.nodes[{index}] is invalid")
            continue
        kind = item.get("__typename")
        if kind == "CheckRun":
            database_id = item.get("databaseId")
            if database_id is not None and (
                type(database_id) is not int or database_id < 1
            ):
                errors.append(f"check run {index} databaseId is invalid")
                database_id = None
            name = _safe_text(
                item.get("name"), f"check run {index} name", errors,
                limit=MAX_NAME_LENGTH,
            )
            status = item.get("status")
            conclusion = item.get("conclusion")
            if status not in VALID_CHECK_STATUSES:
                errors.append(f"check run {index} status is missing or unknown")
                status = None
            if conclusion not in VALID_CHECK_CONCLUSIONS:
                errors.append(f"check run {index} conclusion is unknown")
                conclusion = None
            started_at = _timestamp(
                item.get("startedAt"), f"check run {index} startedAt", errors,
                required=status in {"COMPLETED", "IN_PROGRESS"},
            )
            completed_at = _timestamp(
                item.get("completedAt"), f"check run {index} completedAt", errors,
                required=status == "COMPLETED",
            )
            if status == "COMPLETED" and conclusion is None:
                errors.append(f"check run {index} completed without a conclusion")
            if status != "COMPLETED" and (conclusion is not None or completed_at is not None):
                errors.append(f"check run {index} has contradictory completion fields")
            if started_at and completed_at and completed_at < started_at:
                errors.append(f"check run {index} timestamps are out of order")
            url = _safe_text(
                item.get("detailsUrl"), f"check run {index} detailsUrl", errors,
                limit=MAX_URL_LENGTH, required=False,
            )
            if name is not None and status is not None:
                candidates.append(
                    {
                        "kind": "check-run", "name": name, "status": status,
                        "conclusion": conclusion, "url": url,
                        "started_at": started_at, "completed_at": completed_at,
                        "_database_id": database_id,
                    }
                )
        elif kind == "StatusContext":
            name = _safe_text(
                item.get("context"), f"status context {index} name", errors,
                limit=MAX_NAME_LENGTH,
            )
            state = item.get("state")
            if state not in VALID_STATUS_STATES:
                errors.append(f"status context {index} state is missing or unknown")
                state = None
            created_at = _timestamp(
                item.get("createdAt"), f"status context {index} createdAt", errors,
                required=True,
            )
            url = _safe_text(
                item.get("targetUrl"), f"status context {index} targetUrl", errors,
                limit=MAX_URL_LENGTH, required=False,
            )
            if name is not None and state is not None and created_at is not None:
                candidates.append(
                    {
                        "kind": "status-context", "name": name,
                        "status": (
                            "COMPLETED"
                            if state in {"SUCCESS", "ERROR", "FAILURE"}
                            else "PENDING"
                        ),
                        "conclusion": state, "url": url,
                        "started_at": created_at, "completed_at": None,
                        "_database_id": None,
                    }
                )
        else:
            errors.append(f"status context {index} has an unknown type")
    checks = _select_latest(
        candidates, identity_fields=("kind", "name"), timestamp_field="started_at",
        path="statusCheckRollup.contexts", errors=errors,
        sequence_field="_database_id",
    )
    for check in checks:
        check.pop("_database_id", None)
    if not checks:
        errors.append("statusCheckRollup has no current check contexts")
    else:
        pending = any(
            check["status"] != "COMPLETED" or check["conclusion"] is None
            for check in checks
        )
        failed = any(
            check["conclusion"] in FAILED_CHECK_CONCLUSIONS
            or check["conclusion"] in TRANSIENT_CHECK_CONCLUSIONS
            or check["conclusion"] in {"ERROR", "FAILURE"}
            for check in checks
        )
        all_success = all(
            check["conclusion"] in SUCCESSFUL_CHECK_CONCLUSIONS for check in checks
        )
        contradiction = (
            (failed and rollup_state not in {"ERROR", "FAILURE"})
            or (not failed and pending and rollup_state not in {"EXPECTED", "PENDING"})
            or (all_success and rollup_state != "SUCCESS")
            or (rollup_state in {"ERROR", "FAILURE"} and not failed)
            or (rollup_state in {"EXPECTED", "PENDING"} and not pending)
            or (rollup_state == "SUCCESS" and not all_success)
        )
        if contradiction:
            errors.append("statusCheckRollup.state contradicts its current check contexts")
    return checks, rollup_state if isinstance(rollup_state, str) else None


def _normalize_reviews(
    pr: dict[str, Any], observed_head: str | None, errors: list[str]
) -> dict[str, object]:
    decision = pr.get("reviewDecision")
    if "reviewDecision" not in pr or decision not in VALID_REVIEW_DECISIONS:
        errors.append("pullRequest.reviewDecision is missing or unknown")
        decision = None
    candidates = []
    for index, raw in enumerate(
        _connection_nodes(pr.get("reviews"), "pullRequest.reviews", errors)
    ):
        item = _as_dict(raw)
        if item is None:
            errors.append(f"pullRequest.reviews.nodes[{index}] is invalid")
            continue
        author = _as_dict(item.get("author"))
        login = _safe_text(
            author.get("login") if author else None, f"review {index} author", errors,
            limit=MAX_LOGIN_LENGTH,
        )
        state = item.get("state")
        if state not in VALID_REVIEW_STATES:
            errors.append(f"review {index} state is missing or unknown")
            state = None
        if state == "PENDING":
            continue
        submitted_at = _timestamp(
            item.get("submittedAt"), f"review {index} submittedAt", errors,
            required=True,
        )
        commit = _as_dict(item.get("commit"))
        commit_oid = _oid(
            commit.get("oid") if commit else None, f"review {index} commit oid", errors
        )
        if login and state and submitted_at and commit_oid:
            candidates.append(
                {
                    "author": login, "state": state, "submitted_at": submitted_at,
                    "commit_oid": commit_oid,
                    "applies_to_head": bool(observed_head and commit_oid == observed_head),
                }
            )
    reviews = sorted({_canonical(review): review for review in candidates}.values(), key=_canonical)
    opinionated = [
        review for review in candidates
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
    ]
    for author in sorted({str(review["author"]) for review in opinionated}):
        author_reviews = [review for review in opinionated if review["author"] == author]
        by_time: dict[str, set[str]] = {}
        for review in author_reviews:
            by_time.setdefault(str(review["submitted_at"]), set()).add(_canonical(review))
        if any(len(variants) > 1 for variants in by_time.values()):
            errors.append("pullRequest.reviews has ambiguous attempts with the same timestamp")
    current_decisions = _select_latest(
        [review for review in opinionated if review["applies_to_head"]],
        identity_fields=("author",), timestamp_field="submitted_at",
        path="pullRequest.reviews current-head decisions", errors=errors,
    )
    threads = []
    for index, raw in enumerate(
        _connection_nodes(pr.get("reviewThreads"), "pullRequest.reviewThreads", errors)
    ):
        item = _as_dict(raw)
        if (
            item is None or type(item.get("isResolved")) is not bool
            or type(item.get("isOutdated")) is not bool
        ):
            errors.append(f"review thread {index} has invalid resolution state")
            continue
        thread_id = _safe_text(
            item.get("id"), f"review thread {index} id", errors,
            limit=MAX_THREAD_ID_LENGTH,
        )
        comments = _connection_nodes(
            item.get("comments"), f"review thread {index}.comments", errors,
            limit=1, require_page_info=False,
        )
        if len(comments) != 1 or not isinstance(comments[0], dict):
            errors.append(f"review thread {index} must contain one first comment")
            continue
        first_comment = comments[0]
        author = _as_dict(first_comment.get("author"))
        author_login = _safe_text(
            author.get("login") if author else None,
            f"review thread {index} first comment author", errors,
            limit=MAX_LOGIN_LENGTH, required=False,
        )
        created_at = _timestamp(
            first_comment.get("createdAt"),
            f"review thread {index} first comment createdAt", errors, required=True,
        )
        if thread_id and created_at:
            threads.append(
                {
                    "id": thread_id,
                    "resolved": item["isResolved"], "outdated": item["isOutdated"],
                    "first_comment_author": author_login,
                    "first_comment_at": created_at,
                }
            )
    thread_ids = [str(thread["id"]) for thread in threads]
    if len(thread_ids) != len(set(thread_ids)):
        errors.append("pullRequest.reviewThreads contains duplicate thread ids")
    threads = sorted(threads, key=_canonical)
    current_approvals = [
        review for review in current_decisions if review["state"] == "APPROVED"
    ]
    current_changes_requested = any(
        review["state"] == "CHANGES_REQUESTED" for review in current_decisions
    )
    approval_applies = decision != "APPROVED" or bool(current_approvals)
    return {
        "decision": decision, "items": reviews, "threads": threads,
        "current_head_approval": bool(current_approvals),
        "current_head_changes_requested": current_changes_requested,
        "approval_applies_to_head": approval_applies,
    }


def _fingerprint(result: dict[str, object]) -> str:
    semantic = {
        key: result[key]
        for key in (
            "repository", "pr_number", "observed_head_sha", "expected_head_sha",
            "classification", "checks", "reviews", "merge_state",
            "author_waiting", "product_gate_reasons", "provider_state",
            "provider_errors", "schema_errors", "verdict_reusable",
        )
    }
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _set_fingerprint(result: dict[str, object]) -> None:
    result["fingerprint"] = _fingerprint(result)


def classify_payload(
    repository: str, pr_number: int, payload: dict[str, Any], *,
    expected_head_sha: str | None = None, observed_at: str | None = None,
) -> dict[str, object]:
    validate_repository(repository)
    if type(pr_number) is not int or pr_number < 1:
        raise ValueError("PR number must be a positive integer")
    if not isinstance(payload, dict):
        raise ValueError("GitHub payload must be an object")
    if expected_head_sha is not None:
        expected_head_sha = validate_oid(expected_head_sha, "expected head SHA")
    observation_time = normalize_timestamp(observed_at or utc_now())
    schema_errors: list[str] = []
    provider_errors = _provider_errors(payload)
    data = _as_dict(payload.get("data"))
    provider_state = "complete"
    if provider_errors:
        provider_state = "failed" if payload.get("data") is None else "partial"
    repo = _as_dict(data.get("repository")) if data else None
    pr = _as_dict(repo.get("pullRequest")) if repo else None
    if pr is None:
        schema_errors.append("data.repository.pullRequest is missing or invalid")
        pr = {}
    observed_head = _oid(pr.get("headRefOid"), "pullRequest.headRefOid", schema_errors)
    if type(pr.get("number")) is not int or pr.get("number") != pr_number:
        schema_errors.append("pullRequest.number does not match the requested PR")
    pr_state = pr.get("state")
    if pr_state not in {"OPEN", "CLOSED", "MERGED"}:
        schema_errors.append("pullRequest.state is missing or unknown")
        pr_state = None
    is_draft = pr.get("isDraft")
    if type(is_draft) is not bool:
        schema_errors.append("pullRequest.isDraft is missing or invalid")
        is_draft = None
    mergeable = pr.get("mergeable")
    if mergeable not in VALID_MERGEABLE:
        schema_errors.append("pullRequest.mergeable is missing or unknown")
        mergeable = None
    merge_status = pr.get("mergeStateStatus")
    if merge_status not in VALID_MERGE_STATES:
        schema_errors.append("pullRequest.mergeStateStatus is missing or unknown")
        merge_status = None
    checks, rollup_state = _normalize_checks(pr, observed_head, schema_errors)
    reviews = _normalize_reviews(pr, observed_head, schema_errors)
    unresolved_threads = [
        thread for thread in reviews["threads"]
        if not thread["resolved"] and not thread["outdated"]
    ]
    current_changes_requested = reviews["current_head_changes_requested"]
    blocking_review = current_changes_requested or bool(unresolved_threads)
    author_waiting = bool(is_draft or blocking_review)
    merge_state = {
        "pull_request_state": pr_state, "mergeable": mergeable,
        "status": merge_status, "draft": is_draft,
        "review_decision": reviews["decision"],
        "check_rollup_state": rollup_state,
    }
    pending = any(
        check["status"] != "COMPLETED" or check["conclusion"] is None
        for check in checks
    )
    deterministic_failure = any(
        check["conclusion"] in FAILED_CHECK_CONCLUSIONS
        or check["conclusion"] == "FAILURE" for check in checks
    )
    transient_failure = any(
        check["conclusion"] in TRANSIENT_CHECK_CONCLUSIONS
        or check["conclusion"] == "ERROR" for check in checks
    )
    product_reasons = []
    if pr_state != "OPEN":
        product_reasons.append(f"pull request is {str(pr_state).lower()}")
    if is_draft:
        product_reasons.append("pull request is a draft")
    if reviews["decision"] == "REVIEW_REQUIRED":
        product_reasons.append("required review is pending")
    if reviews["decision"] == "APPROVED" and not reviews["approval_applies_to_head"]:
        product_reasons.append("approval is not bound to the observed head")
    if reviews["decision"] == "CHANGES_REQUESTED" and not current_changes_requested:
        product_reasons.append("review decision lacks current-head evidence")
    if merge_status in PRODUCT_MERGE_STATES:
        product_reasons.append(f"merge state is {str(merge_status).lower()}")
    head_matches = bool(
        observed_head and (
            expected_head_sha is None
            or observed_head.lower() == expected_head_sha.lower()
        )
    )
    review_reusable = bool(
        reviews["decision"] is None
        or (reviews["decision"] == "APPROVED" and reviews["approval_applies_to_head"])
    )
    verdict_reusable = bool(
        head_matches and review_reusable and not provider_errors and not schema_errors
    )
    if observed_head and expected_head_sha and not head_matches:
        classification = "stale-head"
    elif provider_errors or schema_errors:
        classification = "product-gate"
    elif pr_state != "OPEN":
        classification = "product-gate"
    elif mergeable == "CONFLICTING" or merge_status == "DIRTY":
        classification = "conflict"
    elif blocking_review:
        classification = "blocking-review-feedback"
    elif deterministic_failure:
        classification = "deterministic-check-failure"
    elif transient_failure:
        classification = "plausible-transient-failure"
    elif pending:
        classification = "checks-pending"
    elif product_reasons:
        classification = "product-gate"
    elif mergeable == "MERGEABLE" and merge_status == "CLEAN":
        classification = "merge-ready"
    else:
        classification = "product-gate"
        product_reasons.append("GitHub did not report a clean merge state")
    result: dict[str, object] = {
        "repository": repository, "pr_number": pr_number,
        "observed_head_sha": observed_head, "expected_head_sha": expected_head_sha,
        "classification": classification, "checks": checks, "reviews": reviews,
        "merge_state": merge_state, "author_waiting": author_waiting,
        "product_gate_reasons": sorted(set(product_reasons)),
        "provider_state": provider_state, "provider_errors": provider_errors,
        "schema_errors": sorted(set(schema_errors)), "observed_at": observation_time,
        "verdict_reusable": verdict_reusable,
    }
    _set_fingerprint(result)
    return result


def _provider_failure_result(
    repository: str, pr_number: int, expected_head_sha: str | None,
    observed_at: str, errors: list[dict[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {
        "repository": repository, "pr_number": pr_number,
        "observed_head_sha": None, "expected_head_sha": expected_head_sha,
        "classification": "product-gate", "checks": [],
        "reviews": {
            "decision": None, "items": [], "threads": [],
            "current_head_approval": False,
            "current_head_changes_requested": False,
            "approval_applies_to_head": False,
        },
        "merge_state": {
            "pull_request_state": None, "mergeable": None, "status": None,
            "draft": None, "review_decision": None, "check_rollup_state": None,
        },
        "author_waiting": False, "product_gate_reasons": ["GitHub observation failed"],
        "provider_state": "failed", "provider_errors": errors, "schema_errors": [],
        "observed_at": normalize_timestamp(observed_at), "verdict_reusable": False,
    }
    _set_fingerprint(result)
    return result


def observe_with_retry(
    fetch: Callable[[], dict[str, Any]], repository: str, pr_number: int, *,
    expected_head_sha: str | None = None, policy: RetryPolicy | None = None,
    clock: Callable[[], str] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    policy = policy or RetryPolicy()
    retry_trace: list[dict[str, str]] = []
    for attempt in range(1, policy.max_attempts + 1):
        try:
            result = classify_payload(
                repository, pr_number, fetch(), expected_head_sha=expected_head_sha,
                observed_at=clock(),
            )
        except ProviderError as exc:
            provider_error = exc.to_dict()
            if exc.retryable and attempt < policy.max_attempts:
                retry_trace.append({"kind": exc.kind})
                sleep(policy.delay_after(attempt))
                continue
            result = _provider_failure_result(
                repository, pr_number, expected_head_sha, clock(), [provider_error]
            )
            result["attempts"] = attempt
            result["retry_exhausted"] = bool(exc.retryable and attempt == policy.max_attempts)
            result["retry_trace"] = retry_trace
            _set_fingerprint(result)
            return result
        provider_errors = result["provider_errors"]
        retryable_response_error = bool(
            result["provider_state"] == "failed" and provider_errors
            and all(error["retryable"] is True for error in provider_errors)
        )
        if retryable_response_error and attempt < policy.max_attempts:
            retry_trace.append({"kind": "rate-limit"})
            sleep(policy.delay_after(attempt))
            continue
        transient_check = result["classification"] == "plausible-transient-failure"
        if transient_check and attempt < policy.max_attempts:
            retry_trace.append({"kind": "check-transient"})
            sleep(policy.delay_after(attempt))
            continue
        result["attempts"] = attempt
        result["retry_exhausted"] = bool(
            (transient_check or retryable_response_error) and attempt == policy.max_attempts
        )
        result["retry_trace"] = retry_trace
        _set_fingerprint(result)
        return result
    raise AssertionError("retry loop must return")

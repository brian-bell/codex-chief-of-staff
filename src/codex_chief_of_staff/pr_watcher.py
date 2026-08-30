"""Normalize and classify one read-only GitHub pull-request observation."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .github_pr import ProviderError, validate_repository


TRANSIENT_CHECK_CONCLUSIONS = frozenset(
    {"CANCELLED", "STALE", "STARTUP_FAILURE", "TIMED_OUT"}
)
FAILED_CHECK_CONCLUSIONS = frozenset(
    {"ACTION_REQUIRED", "FAILURE", "NEUTRAL", "SKIPPED"}
)
ROLLUP_SUCCESS_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
VALID_REVIEW_DECISIONS = frozenset(
    {"APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}
)
VALID_REVIEW_STATES = frozenset(
    {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"}
)
VALID_CHECK_STATUSES = frozenset(
    {"COMPLETED", "IN_PROGRESS", "PENDING", "QUEUED", "REQUESTED", "WAITING"}
)
VALID_CHECK_CONCLUSIONS = frozenset(
    {
        "ACTION_REQUIRED", "CANCELLED", "FAILURE", "NEUTRAL", "SKIPPED",
        "STALE", "STARTUP_FAILURE", "SUCCESS", "TIMED_OUT",
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


class ObservationError(RuntimeError):
    """A typed failure to acquire or normalize one GitHub observation."""

    def __init__(
        self,
        acquisition_state: str,
        kind: str,
        message: str,
        *,
        provider_errors: Iterable[dict[str, object]] = (),
        schema_errors: Iterable[str] = (),
        retryable: bool = False,
        attempts: int = 1,
        retry_exhausted: bool = False,
        retry_trace: Iterable[dict[str, str]] = (),
    ) -> None:
        if acquisition_state not in {"partial", "unavailable"}:
            raise ValueError("observation errors require partial or unavailable state")
        self.acquisition_state = acquisition_state
        self.kind = kind
        self.safe_message = message
        self.provider_errors = tuple(dict(error) for error in provider_errors)
        self.schema_errors = tuple(schema_errors)
        self.retryable = retryable
        self.attempts = attempts
        self.retry_exhausted = retry_exhausted
        self.retry_trace = tuple(dict(item) for item in retry_trace)
        super().__init__(message)

    def with_attempts(
        self,
        attempts: int,
        retry_trace: Iterable[dict[str, str]],
        *,
        retry_exhausted: bool,
    ) -> ObservationError:
        return ObservationError(
            self.acquisition_state,
            self.kind,
            self.safe_message,
            provider_errors=self.provider_errors,
            schema_errors=self.schema_errors,
            retryable=self.retryable,
            attempts=attempts,
            retry_exhausted=retry_exhausted,
            retry_trace=retry_trace,
        )

    def to_error(self) -> dict[str, object]:
        error: dict[str, object] = {
            "acquisition_state": self.acquisition_state,
            "attempts": self.attempts,
            "kind": self.kind,
            "message": self.safe_message,
            "retry_exhausted": self.retry_exhausted,
        }
        if self.provider_errors:
            error["provider_errors"] = list(self.provider_errors)
        if self.schema_errors:
            error["schema_error_count"] = len(self.schema_errors)
        return {"error": error}


@dataclass(frozen=True)
class NormalizedObservation:
    repository: str
    pr_number: int
    observed_at: str
    observed_head_sha: str
    pull_request_state: str
    draft: bool
    mergeable: str
    merge_status: str
    check_rollup_state: str | None
    checks: tuple[dict[str, object], ...]
    reviews: dict[str, object]


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


def _enum(
    value: object, allowed: frozenset[str], path: str, errors: list[str], *,
    allow_none: bool = False,
) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or value not in allowed:
        errors.append(f"{path} is missing or unknown")
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
    except (ValueError, OverflowError):
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
            continue
        error_type = item.get("type")
        extensions = _as_dict(item.get("extensions"))
        if not isinstance(error_type, str) and extensions is not None:
            error_type = extensions.get("code")
        code = error_type.upper() if isinstance(error_type, str) else "GRAPHQL_ERROR"
        if code == "RATE_LIMITED":
            normalized.add(("rate-limit", "rate-limited", True))
        elif code == "FORBIDDEN":
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


def _provider_error_kind(errors: list[dict[str, object]]) -> str:
    kinds = {str(error["kind"]) for error in errors}
    if "permission" in kinds:
        return "permission"
    if kinds == {"rate-limit"}:
        return "rate-limit"
    return "acquisition"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _select_latest(
    candidates: list[dict[str, object]], *, identity_fields: tuple[str, ...],
    timestamp_field: str, path: str, errors: list[str],
) -> list[dict[str, object]]:
    groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for candidate in candidates:
        identity = tuple(candidate[field] for field in identity_fields)
        groups.setdefault(identity, []).append(candidate)
    selected = []
    for identity in sorted(groups, key=lambda key: tuple(str(item) for item in key)):
        group = groups[identity]
        if len(group) > 1 and any(item[timestamp_field] is None for item in group):
            errors.append(f"{path} has repeated attempts without complete timestamps")
        latest_time = max(str(item[timestamp_field] or "") for item in group)
        latest = [
            item for item in group if str(item[timestamp_field] or "") == latest_time
        ]
        variants = {_canonical(item): item for item in latest}
        if len(variants) > 1:
            errors.append(f"{path} has ambiguous attempts with the same timestamp")
        selected.append(variants[sorted(variants)[0]])
    return sorted(selected, key=_canonical)


def _normalize_checks(
    pr: dict[str, Any], observed_head: str | None, errors: list[str],
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
    if (
        commit_oid is not None and observed_head is not None
        and commit_oid.lower() != observed_head.lower()
    ):
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
    rollup_state = _enum(
        rollup_dict.get("state"), VALID_STATUS_STATES,
        "statusCheckRollup.state", errors,
    )
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
            name = _safe_text(
                item.get("name"), f"check run {index} name", errors,
                limit=MAX_NAME_LENGTH,
            )
            status = _enum(
                item.get("status"), VALID_CHECK_STATUSES,
                f"check run {index} status", errors,
            )
            conclusion = _enum(
                item.get("conclusion"), VALID_CHECK_CONCLUSIONS,
                f"check run {index} conclusion", errors, allow_none=True,
            )
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
            if status is not None and status != "COMPLETED" and (
                conclusion is not None or completed_at is not None
            ):
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
                    }
                )
        elif kind == "StatusContext":
            name = _safe_text(
                item.get("context"), f"status context {index} name", errors,
                limit=MAX_NAME_LENGTH,
            )
            state = _enum(
                item.get("state"), VALID_STATUS_STATES,
                f"status context {index} state", errors,
            )
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
                            "COMPLETED" if state in {"SUCCESS", "ERROR", "FAILURE"}
                            else "PENDING"
                        ),
                        "conclusion": state, "url": url,
                        "started_at": created_at, "completed_at": None,
                    }
                )
        else:
            errors.append(f"status context {index} has an unknown type")
    checks = _select_latest(
        candidates, identity_fields=("kind", "name"),
        timestamp_field="started_at", path="statusCheckRollup.contexts",
        errors=errors,
    )
    if not checks:
        errors.append("statusCheckRollup has no current check contexts")
    else:
        pending = any(
            check["status"] != "COMPLETED" or check["conclusion"] is None
            for check in checks
        )
        rollup_failed = any(
            check["conclusion"] in TRANSIENT_CHECK_CONCLUSIONS
            or check["conclusion"] in {"ACTION_REQUIRED", "ERROR", "FAILURE"}
            for check in checks
        )
        rollup_success = all(
            check["conclusion"] in ROLLUP_SUCCESS_CONCLUSIONS for check in checks
        )
        contradiction = (
            (rollup_failed and rollup_state not in {"ERROR", "FAILURE"})
            or (not rollup_failed and pending and rollup_state not in {"EXPECTED", "PENDING"})
            or (rollup_success and rollup_state != "SUCCESS")
            or (rollup_state in {"ERROR", "FAILURE"} and not rollup_failed)
            or (rollup_state in {"EXPECTED", "PENDING"} and not pending)
            or (rollup_state == "SUCCESS" and not rollup_success)
        )
        if contradiction:
            errors.append("statusCheckRollup.state contradicts its current check contexts")
    return checks, rollup_state


def _normalize_reviews(pr: dict[str, Any], errors: list[str]) -> dict[str, object]:
    decision = _enum(
        pr.get("reviewDecision"), VALID_REVIEW_DECISIONS,
        "pullRequest.reviewDecision", errors, allow_none=True,
    )
    if "reviewDecision" not in pr:
        errors.append("pullRequest.reviewDecision is missing")
    candidates = []
    for index, raw in enumerate(
        _connection_nodes(pr.get("reviews"), "pullRequest.reviews", errors)
    ):
        item = _as_dict(raw)
        if item is None:
            errors.append(f"pullRequest.reviews.nodes[{index}] is invalid")
            continue
        state = _enum(
            item.get("state"), VALID_REVIEW_STATES,
            f"review {index} state", errors,
        )
        if state == "PENDING":
            continue
        author = _as_dict(item.get("author"))
        login = _safe_text(
            author.get("login") if author else None,
            f"review {index} author", errors, limit=MAX_LOGIN_LENGTH,
        )
        submitted_at = _timestamp(
            item.get("submittedAt"), f"review {index} submittedAt", errors,
            required=True,
        )
        commit = _as_dict(item.get("commit"))
        commit_oid = _oid(
            commit.get("oid") if commit else None,
            f"review {index} commit oid", errors,
        )
        if login and state and submitted_at and commit_oid:
            candidates.append(
                {
                    "author": login, "state": state,
                    "submitted_at": submitted_at, "commit_oid": commit_oid,
                }
            )
    reviews = sorted(
        {_canonical(review): review for review in candidates}.values(), key=_canonical
    )
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
                    "id": thread_id, "resolved": item["isResolved"],
                    "outdated": item["isOutdated"],
                    "first_comment_author": author_login,
                    "first_comment_at": created_at,
                }
            )
    thread_ids = [str(thread["id"]) for thread in threads]
    if len(thread_ids) != len(set(thread_ids)):
        errors.append("pullRequest.reviewThreads contains duplicate thread ids")
    return {
        "decision": decision, "items": reviews,
        "threads": sorted(threads, key=_canonical),
    }


def normalize_payload(
    repository: str, pr_number: int, payload: dict[str, Any], *,
    observed_at: str | None = None,
) -> NormalizedObservation:
    """Validate provider JSON and return policy-free PR evidence."""
    validate_repository(repository)
    if type(pr_number) is not int or pr_number < 1:
        raise ValueError("PR number must be a positive integer")
    if not isinstance(payload, dict):
        raise ValueError("GitHub payload must be an object")
    observation_time = normalize_timestamp(observed_at or utc_now())
    provider_errors = _provider_errors(payload)
    if provider_errors:
        state = "unavailable" if payload.get("data") is None else "partial"
        retryable = all(error["retryable"] is True for error in provider_errors)
        raise ObservationError(
            state, _provider_error_kind(provider_errors),
            "GitHub evidence could not be acquired",
            provider_errors=provider_errors, retryable=retryable,
        )
    schema_errors: list[str] = []
    data = _as_dict(payload.get("data"))
    repo = _as_dict(data.get("repository")) if data else None
    pr = _as_dict(repo.get("pullRequest")) if repo else None
    if pr is None:
        schema_errors.append("data.repository.pullRequest is missing or invalid")
        pr = {}
    observed_head = _oid(pr.get("headRefOid"), "pullRequest.headRefOid", schema_errors)
    if type(pr.get("number")) is not int or pr.get("number") != pr_number:
        schema_errors.append("pullRequest.number does not match the requested PR")
    pr_state = _enum(
        pr.get("state"), frozenset({"OPEN", "CLOSED", "MERGED"}),
        "pullRequest.state", schema_errors,
    )
    is_draft = pr.get("isDraft")
    if type(is_draft) is not bool:
        schema_errors.append("pullRequest.isDraft is missing or invalid")
        is_draft = None
    mergeable = _enum(
        pr.get("mergeable"), VALID_MERGEABLE,
        "pullRequest.mergeable", schema_errors,
    )
    merge_status = _enum(
        pr.get("mergeStateStatus"), VALID_MERGE_STATES,
        "pullRequest.mergeStateStatus", schema_errors,
    )
    checks, rollup_state = _normalize_checks(pr, observed_head, schema_errors)
    reviews = _normalize_reviews(pr, schema_errors)
    if schema_errors:
        raise ObservationError(
            "partial", "schema", "GitHub evidence could not be normalized",
            schema_errors=sorted(set(schema_errors)),
        )
    assert observed_head is not None
    assert pr_state is not None
    assert is_draft is not None
    assert mergeable is not None
    assert merge_status is not None
    return NormalizedObservation(
        repository=repository, pr_number=pr_number, observed_at=observation_time,
        observed_head_sha=observed_head, pull_request_state=pr_state,
        draft=is_draft, mergeable=mergeable, merge_status=merge_status,
        check_rollup_state=rollup_state, checks=tuple(checks), reviews=reviews,
    )


def _fingerprint(result: dict[str, object]) -> str:
    semantic = {
        key: result[key]
        for key in (
            "repository", "pr_number", "observed_head_sha", "expected_head_sha",
            "classification", "checks", "reviews", "merge_state",
            "action_owner", "product_gate_reasons", "verdict_reusable",
        )
    }
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _set_fingerprint(result: dict[str, object]) -> None:
    result["fingerprint"] = _fingerprint(result)


def classify_observation(
    observation: NormalizedObservation, *, expected_head_sha: str | None = None,
) -> dict[str, object]:
    """Apply the locked issue policy to validated observation evidence."""
    if expected_head_sha is not None:
        expected_head_sha = validate_oid(expected_head_sha, "expected head SHA")
    checks = list(observation.checks)
    reviews = dict(observation.reviews)
    unresolved_threads = [
        thread for thread in reviews["threads"]
        if not thread["resolved"] and not thread["outdated"]
    ]
    blocking_review = (
        reviews["decision"] == "CHANGES_REQUESTED" or bool(unresolved_threads)
    )
    pending = any(
        check["status"] != "COMPLETED" or check["conclusion"] is None
        for check in checks
    )
    deterministic_failure = any(
        check["conclusion"] in FAILED_CHECK_CONCLUSIONS for check in checks
    )
    transient_failure = any(
        check["conclusion"] in TRANSIENT_CHECK_CONCLUSIONS
        or check["conclusion"] == "ERROR" for check in checks
    )
    head_matches = bool(
        expected_head_sha is None
        or observation.observed_head_sha.lower() == expected_head_sha.lower()
    )
    product_reasons: list[str] = []
    if not head_matches:
        classification, action_owner = "stale-head", "reviewer"
    elif observation.pull_request_state != "OPEN":
        classification, action_owner = "product-gate", "product"
        product_reasons.append(f"pull request is {observation.pull_request_state.lower()}")
    elif observation.draft:
        classification, action_owner = "product-gate", "author"
        product_reasons.append("pull request is a draft")
    elif observation.mergeable == "CONFLICTING" or observation.merge_status == "DIRTY":
        classification, action_owner = "conflict", "author"
    elif blocking_review:
        classification, action_owner = "blocking-review-feedback", "author"
    elif deterministic_failure:
        classification, action_owner = "deterministic-check-failure", "author"
    elif transient_failure:
        classification, action_owner = "plausible-transient-failure", "ci"
    elif pending:
        classification, action_owner = "checks-pending", "ci"
    elif reviews["decision"] == "REVIEW_REQUIRED":
        classification, action_owner = "product-gate", "reviewer"
        product_reasons.append("required review is pending")
    elif observation.merge_status in PRODUCT_MERGE_STATES:
        classification, action_owner = "product-gate", "product"
        product_reasons.append(f"merge state is {observation.merge_status.lower()}")
    elif observation.mergeable == "MERGEABLE" and observation.merge_status == "CLEAN":
        classification, action_owner = "merge-ready", "none"
    else:
        classification, action_owner = "product-gate", "product"
        product_reasons.append("GitHub did not report a clean merge state")
    merge_state = {
        "pull_request_state": observation.pull_request_state,
        "mergeable": observation.mergeable,
        "status": observation.merge_status,
        "draft": observation.draft,
        "review_decision": reviews["decision"],
        "check_rollup_state": observation.check_rollup_state,
    }
    result: dict[str, object] = {
        "acquisition_state": "complete",
        "repository": observation.repository,
        "pr_number": observation.pr_number,
        "observed_head_sha": observation.observed_head_sha,
        "expected_head_sha": expected_head_sha,
        "classification": classification,
        "checks": checks,
        "reviews": reviews,
        "merge_state": merge_state,
        "action_owner": action_owner,
        "product_gate_reasons": sorted(set(product_reasons)),
        "observed_at": observation.observed_at,
        "verdict_reusable": bool(expected_head_sha is not None and head_matches),
    }
    _set_fingerprint(result)
    return result


def classify_payload(
    repository: str, pr_number: int, payload: dict[str, Any], *,
    expected_head_sha: str | None = None, observed_at: str | None = None,
) -> dict[str, object]:
    observation = normalize_payload(
        repository, pr_number, payload, observed_at=observed_at
    )
    return classify_observation(observation, expected_head_sha=expected_head_sha)


def _provider_observation_error(error: ProviderError) -> ObservationError:
    provider_error = error.to_dict()
    provider_error["code"] = error.kind
    return ObservationError(
        "unavailable", error.kind, "GitHub evidence could not be acquired",
        provider_errors=[provider_error], retryable=error.retryable,
    )


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
                repository, pr_number, fetch(),
                expected_head_sha=expected_head_sha, observed_at=clock(),
            )
        except ProviderError as error:
            observation_error = _provider_observation_error(error)
        except ObservationError as error:
            observation_error = error
        else:
            result["attempts"] = attempt
            result["retry_exhausted"] = False
            result["retry_trace"] = retry_trace
            _set_fingerprint(result)
            return result
        if observation_error.retryable and attempt < policy.max_attempts:
            retry_trace.append({"kind": observation_error.kind})
            sleep(policy.delay_after(attempt))
            continue
        raise observation_error.with_attempts(
            attempt, retry_trace,
            retry_exhausted=bool(
                observation_error.retryable and attempt == policy.max_attempts
            ),
        )
    raise AssertionError("retry loop must return or raise")

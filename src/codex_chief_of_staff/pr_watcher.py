"""Deterministic, read-only pull-request observation and classification."""

from __future__ import annotations

import hashlib
import heapq
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import urlsplit

from .github_pr import (
    _connection_node_is_valid,
    _connection_schema_is_valid,
    _graphql_errors_schema_is_valid,
    _invalid_pr_scalar_fields,
    _nullable_string,
    _parse_iso_instant,
    _pr_scalar_is_valid,
)


Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
Fetcher = Callable[[str, int], Mapping[str, Any]]


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry settings for explicitly transient observations."""

    max_retries: int = 2
    backoff_seconds: tuple[float, ...] = (1.0, 2.0)

    def __post_init__(self) -> None:
        if self.max_retries < 0 or self.max_retries > 5:
            raise ValueError("max_retries must be between 0 and 5")
        if len(self.backoff_seconds) < self.max_retries:
            raise ValueError("backoff_seconds must cover every retry")
        if any(delay < 0 or delay > 60 for delay in self.backoff_seconds):
            raise ValueError("retry delays must be between 0 and 60 seconds")

_FAILURE_CONCLUSIONS = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "FAILURE",
    "NEUTRAL",
    "SKIPPED",
    "STALE",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}
_PENDING_STATUSES = {"EXPECTED", "IN_PROGRESS", "PENDING", "QUEUED", "REQUESTED", "WAITING"}
_STATUS_CONTEXT_FAILURES = {"ERROR", "FAILURE"}
_AUTHOR_WAITING_LABELS = {"awaiting-author", "needs-author", "waiting-for-author"}
_PRODUCT_GATE_LABELS = {"do-not-merge", "hold", "needs-decision", "needs-product", "product-gate"}
_MAX_NORMALIZED_ERRORS = 16
_MAX_ERROR_PATH_PARTS = 8
_MAX_ERROR_TEXT = 80
_MAX_INLINE_ERROR_INTEGER = 10 ** _MAX_ERROR_TEXT
_ERROR_INTEGER_CHUNK_BYTES = 4_096


def _iso_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprint(result: Mapping[str, Any]) -> str:
    telemetry = {"fingerprint", "observation_time", "rate_limit", "retry"}
    content = {key: value for key, value in result.items() if key not in telemetry}
    encoded = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _bounded(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value[: limit + 1].split())[:limit]


def _bounded_error_path_part(value: str | int) -> str:
    if isinstance(value, str):
        return _bounded(value, _MAX_ERROR_TEXT) or ""
    if value < _MAX_INLINE_ERROR_INTEGER:
        return str(value)
    digest = hashlib.sha256()
    remaining = value
    chunk_bits = _ERROR_INTEGER_CHUNK_BYTES * 8
    chunk_mask = (1 << chunk_bits) - 1
    while remaining:
        chunk = remaining & chunk_mask
        chunk_bytes = min(
            _ERROR_INTEGER_CHUNK_BYTES,
            (remaining.bit_length() + 7) // 8,
        )
        digest.update(chunk.to_bytes(chunk_bytes, "little"))
        remaining >>= chunk_bits
    return f"<int:sha256:{digest.hexdigest()}>"


def _url_host(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    return parsed.hostname


def _check_outcome(item: Mapping[str, Any]) -> str:
    kind = item.get("__typename")
    if kind == "CheckRun":
        status = item.get("status")
        if status in _PENDING_STATUSES:
            return "pending"
        if status != "COMPLETED":
            return "incomplete"
        conclusion = item.get("conclusion")
        if conclusion == "SUCCESS":
            return "success"
        if conclusion in _FAILURE_CONCLUSIONS:
            return "failure"
        return "incomplete"
    if kind == "StatusContext":
        state = item.get("state")
        if state == "SUCCESS":
            return "success"
        if state == "PENDING":
            return "pending"
        if state in _STATUS_CONTEXT_FAILURES:
            return "failure"
        return "incomplete"
    return "incomplete"


def _attempt_key(item: Mapping[str, Any], outcome: str) -> tuple[Any, ...]:
    timestamp = max(
        (
            parsed
            for value in (
                item.get("completedAt"),
                item.get("startedAt"),
                item.get("createdAt"),
            )
            if (parsed := _parse_iso_instant(value)) is not None
        ),
        default=datetime.min.replace(tzinfo=timezone.utc),
    )
    database_id = item.get("databaseId")
    attempt = database_id if isinstance(database_id, int) and not isinstance(database_id, bool) else -1
    conservative_rank = {"success": 0, "incomplete": 1, "failure": 2, "pending": 3}[outcome]
    output = item.get("output")
    output = output if isinstance(output, Mapping) else item
    name = item.get("name") or item.get("context")
    stable = json.dumps(
        {
            "annotations_count": output.get("annotationsCount"),
            "attempt_id": database_id,
            "completed_at": item.get("completedAt"),
            "conclusion": item.get("conclusion"),
            "details_host": _url_host(item.get("detailsUrl") or item.get("targetUrl")),
            "name": name,
            "outcome": outcome,
            "started_at": item.get("startedAt") or item.get("createdAt"),
            "status": item.get("status") or item.get("state"),
            "type": item.get("__typename"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return timestamp, attempt, conservative_rank, stable


def classify_github_response(
    repository: str,
    pull_request: int,
    payload: Mapping[str, Any],
    *,
    expected_head: str | None = None,
    verdict_head: str | None = None,
    clock: Clock,
    retry_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize a saved GitHub GraphQL response and classify the PR."""

    data = payload.get("data") if isinstance(payload, Mapping) else None
    data = data if isinstance(data, Mapping) else {}
    repository_data = data.get("repository")
    repository_data = repository_data if isinstance(repository_data, Mapping) else {}
    pr_data = repository_data.get("pullRequest")
    pr = pr_data if isinstance(pr_data, Mapping) else {}
    invalid_pr_scalars = _invalid_pr_scalar_fields(pr)
    head = pr.get("headRefOid") if _pr_scalar_is_valid("headRefOid", pr.get("headRefOid")) else None
    base_branch = pr.get("baseRefName") if _pr_scalar_is_valid("baseRefName", pr.get("baseRefName")) else None
    draft = pr.get("isDraft") if _pr_scalar_is_valid("isDraft", pr.get("isDraft")) else None
    merge_state_status = pr.get("mergeStateStatus") if _pr_scalar_is_valid("mergeStateStatus", pr.get("mergeStateStatus")) else None
    mergeable = pr.get("mergeable") if _pr_scalar_is_valid("mergeable", pr.get("mergeable")) else None
    pr_state = pr.get("state") if _pr_scalar_is_valid("state", pr.get("state")) else None
    review_decision = pr.get("reviewDecision")
    valid_review_decision = (
        "reviewDecision" in pr and _nullable_string(review_decision)
    )
    raw_rollup = pr.get("statusCheckRollup")
    rollup_is_null = "statusCheckRollup" in pr and raw_rollup is None
    rollup = raw_rollup if isinstance(raw_rollup, Mapping) else {}
    contexts = (
        {
            "nodes": [],
            "pageInfo": {"hasNextPage": False, "hasPreviousPage": False},
            "totalCount": 0,
        }
        if rollup_is_null
        else rollup.get("contexts")
    )
    contexts = contexts if isinstance(contexts, Mapping) else {}
    check_nodes = contexts.get("nodes")
    check_nodes = (
        [node for node in check_nodes if _connection_node_is_valid("checks", node)]
        if isinstance(check_nodes, list)
        else []
    )

    def check_output(item: Mapping[str, Any]) -> Mapping[str, Any]:
        output = item.get("output")
        if isinstance(output, Mapping):
            return output
        return item

    attempts: dict[tuple[str, str], list[tuple[Mapping[str, Any], str]]] = {}
    for item in check_nodes:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name") or item.get("context")
        valid_name = isinstance(name, str) and bool(name)
        identity = str(name) if valid_name else "<missing-name>"
        kind = str(item.get("__typename") or "<missing-type>")
        outcome = _check_outcome(item) if valid_name else "incomplete"
        attempts.setdefault((kind, identity), []).append((item, outcome))
    checks = []
    for (_kind, identity), candidates in attempts.items():
        selected, outcome = max(
            candidates, key=lambda candidate: _attempt_key(candidate[0], candidate[1])
        )
        checks.append(
            {
                "attempt_id": selected.get("databaseId"),
                "completed_at": selected.get("completedAt"),
                "conclusion": selected.get("conclusion"),
                "details_host": _url_host(
                    selected.get("detailsUrl") or selected.get("targetUrl")
                ),
                "duplicate_attempts": len(candidates),
                "name": identity,
                "outcome": outcome,
                "output": {
                    "annotations_count": check_output(selected).get("annotationsCount"),
                },
                "started_at": selected.get("startedAt") or selected.get("createdAt"),
                "status": selected.get("status") or selected.get("state"),
                "type": selected.get("__typename"),
            }
        )
    checks.sort(key=lambda item: (str(item["name"]), str(item["type"])))
    raw_review_nodes = (
        (pr.get("reviews") or {}).get("nodes", [])
        if isinstance(pr.get("reviews"), Mapping)
        else []
    )
    review_nodes = [
        node
        for node in raw_review_nodes
        if _connection_node_is_valid("reviews", node)
    ]
    reviews_by_author: dict[str, list[Mapping[str, Any]]] = {}
    for item in review_nodes:
        if not isinstance(item, Mapping):
            continue
        author = item.get("author")
        login = author.get("login") if isinstance(author, Mapping) else None
        identity = login if isinstance(login, str) and login else f"<missing-author:{item.get('id')}>"
        reviews_by_author.setdefault(identity, []).append(item)
    def review_tie_rank(item: Mapping[str, Any]) -> int:
        state = item.get("state")
        commit = item.get("commit")
        commit_head = commit.get("oid") if isinstance(commit, Mapping) else None
        if state == "CHANGES_REQUESTED" and commit_head == head:
            return 6
        if state not in {"COMMENTED", "APPROVED", "DISMISSED", "CHANGES_REQUESTED"}:
            return 5
        if not isinstance(commit_head, str) or not commit_head:
            return 5
        if commit_head != head:
            return 4
        if state == "DISMISSED":
            return 3
        if state == "APPROVED":
            return 2
        return 1

    def review_semantic_key(item: Mapping[str, Any]) -> str:
        author = item.get("author")
        commit = item.get("commit")
        return json.dumps(
            {
                "author": author.get("login") if isinstance(author, Mapping) else None,
                "commit_head": commit.get("oid") if isinstance(commit, Mapping) else None,
                "id": item.get("id"),
                "state": item.get("state"),
                "submitted_at": item.get("submittedAt"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    reviews = []
    for author, candidates in reviews_by_author.items():
        selected = max(
            candidates,
            key=lambda item: (
                _parse_iso_instant(item.get("submittedAt"))
                or datetime.min.replace(tzinfo=timezone.utc),
                review_tie_rank(item),
                str(item.get("id") or ""),
                review_semantic_key(item),
            ),
        )
        commit = selected.get("commit")
        commit_head = commit.get("oid") if isinstance(commit, Mapping) else None
        reviews.append(
            {
                "author": None if author.startswith("<missing-author:") else author,
                "commit_head": commit_head,
                "id": selected.get("id"),
                "state": selected.get("state"),
                "submitted_at": selected.get("submittedAt"),
            }
        )
    reviews.sort(key=lambda item: (str(item["author"]), str(item["id"])))
    unresolved_threads = sorted(
        (
            {
                "id": thread.get("id"),
                "is_outdated": bool(thread.get("isOutdated")),
                "is_resolved": bool(thread.get("isResolved")),
            }
            for thread in (
                (pr.get("reviewThreads") or {}).get("nodes", [])
                if isinstance(pr.get("reviewThreads"), Mapping)
                else []
            )
            if _connection_node_is_valid("review_threads", thread)
            if not thread.get("isResolved") and not thread.get("isOutdated")
        ),
        key=lambda item: str(item["id"]),
    )
    label_connection = pr.get("labels")
    label_nodes = (
        label_connection.get("nodes", [])
        if isinstance(label_connection, Mapping)
        else []
    )
    labels = sorted(
        str(item.get("name", "")).lower()
        for item in label_nodes
        if _connection_node_is_valid("labels", item)
    )
    author_signals = [label for label in labels if label in _AUTHOR_WAITING_LABELS]
    product_signals = [label for label in labels if label in _PRODUCT_GATE_LABELS]
    errors_schema_valid = _graphql_errors_schema_is_valid(payload)
    raw_errors = payload.get("errors", []) if errors_schema_valid else []
    normalized_errors = (
        {
            "path": [
                _bounded_error_path_part(part)
                for part in item.get("path", [])[:_MAX_ERROR_PATH_PARTS]
            ],
            "type": _bounded(item.get("type"), _MAX_ERROR_TEXT) or "UNKNOWN",
        }
        for item in raw_errors
    )
    errors = heapq.nsmallest(
        _MAX_NORMALIZED_ERRORS,
        normalized_errors,
        key=lambda item: (item["type"], item["path"]),
    )
    missing = sorted(
        invalid_pr_scalars
        + ([] if errors_schema_valid else ["errors"])
        + ([] if valid_review_decision else ["reviewDecision"])
        + (
            []
            if rollup_is_null
            or (
                isinstance(raw_rollup, Mapping)
                and _pr_scalar_is_valid("state", rollup.get("state"))
            )
            else ["statusCheckRollup.state"]
        )
        + [
            key
            for key in ("labels", "reviews", "reviewThreads")
            if not isinstance(pr.get(key), Mapping)
        ]
        + (["statusCheckRollup"] if "statusCheckRollup" not in pr else [])
        + (["branchProtectionRules"] if not isinstance(repository_data.get("branchProtectionRules"), Mapping) else [])
        + (["rateLimit"] if not isinstance(data.get("rateLimit"), Mapping) else [])
    )
    paginated = []
    incomplete_connections = []
    for name, connection in (
        ("checks", contexts),
        ("reviews", pr.get("reviews")),
        ("review_threads", pr.get("reviewThreads")),
        ("labels", pr.get("labels")),
        ("branch_protection", repository_data.get("branchProtectionRules")),
    ):
        if not isinstance(connection, Mapping):
            incomplete_connections.append(name)
            continue
        page_info = connection.get("pageInfo")
        if not _connection_schema_is_valid(name, connection):
            incomplete_connections.append(name)
        if isinstance(page_info, Mapping) and (
            page_info.get("hasNextPage") is True
            or page_info.get("hasPreviousPage") is True
        ):
            paginated.append(name)
    partial_active = bool(errors or missing or paginated or incomplete_connections)
    rate_limit = data.get("rateLimit") if isinstance(data.get("rateLimit"), Mapping) else None
    transient_acquisition = bool(raw_errors) and all(
        item["type"] == "RATE_LIMITED" for item in raw_errors
    )
    protection = repository_data.get("branchProtectionRules")
    raw_protection_nodes = (
        protection.get("nodes", []) if isinstance(protection, Mapping) else []
    )
    protection_nodes = [
        node
        for node in raw_protection_nodes
        if _connection_node_is_valid("branch_protection", node)
    ]
    branch_protection = sorted(
        (
            {
                "pattern": item.get("pattern"),
                "required_approving_review_count": item.get("requiredApprovingReviewCount"),
                "required_status_checks": sorted(item.get("requiredStatusCheckContexts") or []),
                "requires_approving_reviews": item.get("requiresApprovingReviews"),
                "requires_status_checks": item.get("requiresStatusChecks"),
            }
            for item in protection_nodes
        ),
        key=lambda item: str(item["pattern"]),
    )
    applicable_protection = [
        rule
        for rule in branch_protection
        if isinstance(base_branch, str)
        and isinstance(rule["pattern"], str)
        and fnmatchcase(base_branch, rule["pattern"])
    ]
    required_checks = sorted(
        {
            name
            for rule in applicable_protection
            if rule["requires_status_checks"]
            for name in rule["required_status_checks"]
        }
    )
    required_review = any(
        rule["requires_approving_reviews"]
        and (rule["required_approving_review_count"] or 0) > 0
        for rule in applicable_protection
    )
    required_approval_count = max(
        (
            rule["required_approving_review_count"] or 0
            for rule in applicable_protection
            if rule["requires_approving_reviews"]
        ),
        default=0,
    )
    current_approvals = [
        item
        for item in reviews
        if item["state"] == "APPROVED" and item["commit_head"] == head
    ]
    current_changes_requested = [
        item
        for item in reviews
        if item["state"] == "CHANGES_REQUESTED" and item["commit_head"] == head
    ]
    incomplete_review_evidence = [
        item
        for item in reviews
        if item["state"] in {"APPROVED", "CHANGES_REQUESTED"}
        and (not item["author"] or not item["commit_head"])
    ]
    checks_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for item in checks:
        checks_by_name.setdefault(str(item["name"]), []).append(item)
    waiting_required_checks = [
        name
        for name in required_checks
        if name not in checks_by_name
    ]
    incomplete_checks = [item for item in checks if item["outcome"] == "incomplete"]
    pending_checks = [item for item in checks if item["outcome"] == "pending"]
    failed_checks = [item for item in checks if item["outcome"] == "failure"]
    rollup_state = rollup.get("state")
    if rollup_is_null:
        rollup_outcome = "success"
    elif rollup_state == "SUCCESS":
        rollup_outcome = "success"
    elif rollup_state in {"EXPECTED", "PENDING"}:
        rollup_outcome = "pending"
    elif rollup_state in {"ERROR", "FAILURE"}:
        rollup_outcome = "failure"
    else:
        rollup_outcome = "incomplete"
    if incomplete_checks:
        child_outcome = "incomplete"
    elif pending_checks:
        child_outcome = "pending"
    elif failed_checks:
        child_outcome = "failure"
    elif checks:
        child_outcome = "success"
    else:
        child_outcome = None
    if rollup_outcome == "incomplete" or child_outcome == "incomplete":
        effective_check_outcome = "incomplete"
    elif rollup_outcome == "failure":
        effective_check_outcome = "failure"
    elif rollup_outcome == "pending":
        effective_check_outcome = "pending"
    else:
        effective_check_outcome = child_outcome or "success"
    result: dict[str, Any] = {
        "author_waiting": {"active": bool(author_signals), "signals": author_signals},
        "base_branch": base_branch,
        "branch_protection": branch_protection,
        "checks": checks,
        "classification": "merge-ready",
        "counts": {
            "conversation_comments": (
                pr.get("comments", {}).get("totalCount")
                if isinstance(pr.get("comments"), Mapping)
                else None
            ),
            "review_threads": (
                pr.get("reviewThreads", {}).get("totalCount")
                if isinstance(pr.get("reviewThreads"), Mapping)
                else None
            ),
            "reviews": (
                pr.get("reviews", {}).get("totalCount")
                if isinstance(pr.get("reviews"), Mapping)
                else None
            ),
        },
        "draft": draft,
        "expected_head": expected_head,
        "merge_state_status": merge_state_status,
        "mergeable": mergeable,
        "observation_time": _iso_time(clock()),
        "observed_head_sha": head,
        "partial_response": {
            "active": partial_active,
            "error_count": len(raw_errors),
            "errors": errors,
            "errors_truncated": len(raw_errors) > len(errors),
            "incomplete_connections": sorted(set(incomplete_connections)),
            "missing_fields": missing,
            "truncated_connections": sorted(paginated),
        },
        "permissions": {
            "branch_protection": "available" if isinstance(protection, Mapping) else "unavailable",
            "review_threads": "available" if isinstance(pr.get("reviewThreads"), Mapping) else "unavailable",
        },
        "product_gate": {"active": bool(product_signals), "signals": product_signals},
        "pull_request": pull_request,
        "rate_limit": rate_limit,
        "reason": "open, approved, conflict-free pull request has successful checks",
        "repository": repository,
        "retry": dict(retry_evidence or {"attempts": 1, "delays_seconds": [], "exhausted": False}),
        "review_decision": review_decision if valid_review_decision else None,
        "reviews": reviews,
        "state": pr_state,
        "unresolved_threads": unresolved_threads,
        "verdict_applies": bool(verdict_head and verdict_head == head),
        "verdict_head": verdict_head,
    }
    stale_constraints = [
        (name, value)
        for name, value in (
            ("expected head", expected_head),
            ("verdict head", verdict_head),
        )
        if value is not None and head is not None and value != head
    ]
    if stale_constraints:
        result["classification"] = "stale-head"
        result["reason"] = (
            f"observed head {head} differs from "
            + ", ".join(f"{name} {value}" for name, value in stale_constraints)
        )
    elif mergeable == "CONFLICTING" or merge_state_status == "DIRTY":
        result["classification"] = "conflict"
        result["reason"] = "GitHub reports the pull request as conflicting"
    elif current_changes_requested or unresolved_threads:
        result["classification"] = "blocking-review-feedback"
        result["reason"] = "changes are requested or an active review thread is unresolved"
    elif (
        (required_review and len(current_approvals) < required_approval_count)
        or review_decision == "REVIEW_REQUIRED"
        or bool(incomplete_review_evidence)
        or (effective_check_outcome == "incomplete" and not transient_acquisition)
        or mergeable == "UNKNOWN"
        or merge_state_status == "UNKNOWN"
        or (partial_active and not transient_acquisition)
        or (pr_state is not None and pr_state != "OPEN")
        or draft is True
        or author_signals
        or product_signals
    ):
        result["classification"] = "product-gate"
        if pr_state and pr_state != "OPEN":
            result["product_gate"]["signals"].append(f"state:{pr_state.lower()}")
        if draft is True:
            result["product_gate"]["signals"].append("draft")
        if required_review and len(current_approvals) < required_approval_count:
            result["product_gate"]["signals"].append("required-review")
        elif review_decision == "REVIEW_REQUIRED":
            result["product_gate"]["signals"].append("required-review")
        if mergeable == "UNKNOWN" or merge_state_status == "UNKNOWN":
            result["product_gate"]["signals"].append("mergeability-unknown")
        if partial_active:
            result["product_gate"]["signals"].append("incomplete-evidence")
        if effective_check_outcome == "incomplete":
            result["product_gate"]["signals"].append("incomplete-check-evidence")
        if incomplete_review_evidence:
            result["product_gate"]["signals"].append("incomplete-review-evidence")
        result["product_gate"]["active"] = True
        result["reason"] = (
            "GitHub evidence is incomplete"
            if partial_active
            else "pull request state requires an author or product decision"
        )
    elif effective_check_outcome == "pending" or waiting_required_checks:
        result["classification"] = "checks-pending"
        result["reason"] = (
            f"required checks are missing or pending: {','.join(waiting_required_checks)}"
            if waiting_required_checks
            else "at least one check has not completed"
        )
    else:
        if transient_acquisition:
            result["classification"] = "plausible-transient-failure"
            result["reason"] = "GitHub reported an explicit transient acquisition or rate-limit signal"
        elif effective_check_outcome == "failure":
            result["classification"] = "deterministic-check-failure"
            result["reason"] = "a failed check lacks explicit transient-infrastructure evidence"
        elif merge_state_status != "CLEAN" or mergeable != "MERGEABLE":
            result["classification"] = "product-gate"
            result["product_gate"]["active"] = True
            result["product_gate"]["signals"].append("merge-state-not-clean")
            result["reason"] = "GitHub does not report a clean, mergeable pull request"
    result["fingerprint"] = _fingerprint(result)
    return result


def observe_with_retry(
    fetcher: Fetcher,
    repository: str,
    pull_request: int,
    *,
    policy: RetryPolicy = RetryPolicy(),
    clock: Clock,
    sleeper: Sleeper,
    expected_head: str | None = None,
    verdict_head: str | None = None,
) -> dict[str, Any]:
    """Fetch once, retry only explicit transient failures, and always stop."""

    delays: list[float] = []
    for attempt in range(1, policy.max_retries + 2):
        payload = fetcher(repository, pull_request)
        result = classify_github_response(
            repository,
            pull_request,
            payload,
            expected_head=expected_head,
            verdict_head=verdict_head,
            clock=clock,
            retry_evidence={
                "attempts": attempt,
                "delays_seconds": delays,
                "exhausted": False,
            },
        )
        if result["classification"] != "plausible-transient-failure":
            return result
        if attempt > policy.max_retries:
            result["retry"]["exhausted"] = True
            result["fingerprint"] = _fingerprint(result)
            return result
        delay = policy.backoff_seconds[attempt - 1]
        delays.append(delay)
        sleeper(delay)
    raise AssertionError("bounded retry loop did not return")

"""Narrow, read-only GitHub CLI adapter for pull-request observation."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}$")

GRAPHQL_QUERY = """query WatchPullRequest($owner:String!,$name:String!,$number:Int!){rateLimit{cost remaining resetAt} repository(owner:$owner,name:$name){branchProtectionRules(first:100){totalCount nodes{pattern requiredApprovingReviewCount requiredStatusCheckContexts requiresApprovingReviews requiresStatusChecks} pageInfo{hasNextPage hasPreviousPage}} pullRequest(number:$number){baseRefName comments{totalCount} headRefOid isDraft labels(first:100){totalCount nodes{name} pageInfo{hasNextPage hasPreviousPage}} mergeStateStatus mergeable reviewDecision reviewThreads(first:100){totalCount nodes{id isOutdated isResolved} pageInfo{hasNextPage hasPreviousPage}} reviews(last:100){totalCount nodes{id state submittedAt author{login} commit{oid}} pageInfo{hasNextPage hasPreviousPage}} state statusCheckRollup{state contexts(first:100){totalCount nodes{__typename ... on CheckRun{databaseId name status conclusion startedAt completedAt detailsUrl} ... on StatusContext{id context state createdAt targetUrl}} pageInfo{hasNextPage hasPreviousPage}}}}}}"""


class UnsafeGitHubCommand(ValueError):
    """Raised before any GitHub command outside the fixed query can execute."""


class GitHubAcquisitionError(RuntimeError):
    kind = "acquisition"
    exit_code = 3
    safe_message = "GitHub acquisition failed"

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class GitHubPermissionError(GitHubAcquisitionError):
    kind = "permission"
    exit_code = 4
    safe_message = "GitHub did not permit the required read-only observation"


class GitHubMalformedResponseError(GitHubAcquisitionError):
    kind = "malformed-response"
    exit_code = 5
    safe_message = "GitHub returned malformed JSON"


class GitHubSchemaError(GitHubAcquisitionError):
    kind = "schema"
    exit_code = 5
    safe_message = "GitHub response omitted required observation fields"


class GitHubTransientError(GitHubAcquisitionError):
    kind = "transient-exhausted"
    exit_code = 6
    safe_message = "GitHub transient acquisition retries were exhausted"


Executor = Callable[..., subprocess.CompletedProcess[str]]


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _nullable_string(value: Any) -> bool:
    return value is None or _nonempty_string(value)


def _parse_iso_instant(value: Any) -> datetime | None:
    if not _nonempty_string(value):
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _nullable_instant(value: Any) -> bool:
    return value is None or _parse_iso_instant(value) is not None


def _exact_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _exact_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _pr_scalar_is_valid(name: str, value: Any) -> bool:
    if name == "isDraft":
        return isinstance(value, bool)
    if name == "headRefOid":
        return isinstance(value, str) and _OBJECT_ID.fullmatch(value) is not None
    return _nonempty_string(value)


def _invalid_pr_scalar_fields(pr: Mapping[str, Any]) -> list[str]:
    return [
        name
        for name in (
            "baseRefName",
            "headRefOid",
            "isDraft",
            "mergeStateStatus",
            "mergeable",
            "state",
        )
        if name not in pr or not _pr_scalar_is_valid(name, pr.get(name))
    ]


def _connection_node_is_valid(name: str, node: Any) -> bool:
    if not isinstance(node, Mapping) or not node:
        return False
    if name == "reviews":
        author = node.get("author")
        commit = node.get("commit")
        return (
            _nonempty_string(node.get("id"))
            and _nonempty_string(node.get("state"))
            and _parse_iso_instant(node.get("submittedAt")) is not None
            and isinstance(author, Mapping)
            and _nonempty_string(author.get("login"))
            and isinstance(commit, Mapping)
            and _nonempty_string(commit.get("oid"))
        )
    if name == "review_threads":
        return (
            _nonempty_string(node.get("id"))
            and isinstance(node.get("isOutdated"), bool)
            and isinstance(node.get("isResolved"), bool)
        )
    if name == "labels":
        return _nonempty_string(node.get("name"))
    if name == "branch_protection":
        checks = node.get("requiredStatusCheckContexts")
        return (
            _nonempty_string(node.get("pattern"))
            and _exact_nonnegative_int(node.get("requiredApprovingReviewCount"))
            and isinstance(checks, list)
            and all(_nonempty_string(item) for item in checks)
            and isinstance(node.get("requiresApprovingReviews"), bool)
            and isinstance(node.get("requiresStatusChecks"), bool)
        )
    if name != "checks":
        return False
    kind = node.get("__typename")
    if kind == "CheckRun":
        output = node.get("output")
        valid_output = output is None or (
            isinstance(output, Mapping)
            and (
                "annotationsCount" not in output
                or output.get("annotationsCount") is None
                or _exact_nonnegative_int(output.get("annotationsCount"))
            )
        )
        return (
            _exact_nonnegative_int(node.get("databaseId"))
            and _nonempty_string(node.get("name"))
            and _nonempty_string(node.get("status"))
            and "conclusion" in node
            and _nullable_string(node.get("conclusion"))
            and "startedAt" in node
            and _nullable_instant(node.get("startedAt"))
            and "completedAt" in node
            and _nullable_instant(node.get("completedAt"))
            and "detailsUrl" in node
            and _nullable_string(node.get("detailsUrl"))
            and valid_output
        )
    if kind == "StatusContext":
        return (
            _nonempty_string(node.get("id"))
            and _nonempty_string(node.get("context"))
            and _nonempty_string(node.get("state"))
            and _parse_iso_instant(node.get("createdAt")) is not None
            and "targetUrl" in node
            and _nullable_string(node.get("targetUrl"))
        )
    return False


def _connection_schema_is_valid(name: str, connection: Any) -> bool:
    if not isinstance(connection, Mapping):
        return False
    nodes = connection.get("nodes")
    page_info = connection.get("pageInfo")
    total_count = connection.get("totalCount")
    return (
        isinstance(nodes, list)
        and len(nodes) <= 100
        and all(_connection_node_is_valid(name, node) for node in nodes)
        and isinstance(page_info, Mapping)
        and isinstance(page_info.get("hasNextPage"), bool)
        and isinstance(page_info.get("hasPreviousPage"), bool)
        and _exact_nonnegative_int(total_count)
        and total_count == len(nodes)
    )


def _graphql_error_is_valid(error: Any) -> bool:
    if not isinstance(error, Mapping) or not _nonempty_string(error.get("type")):
        return False
    if "message" in error and not isinstance(error.get("message"), str):
        return False
    if "path" in error:
        path = error.get("path")
        if not isinstance(path, list) or not all(
            _nonempty_string(part) or _exact_nonnegative_int(part) for part in path
        ):
            return False
    if "extensions" in error:
        extensions = error.get("extensions")
        if not isinstance(extensions, Mapping) or not all(
            isinstance(key, str) for key in extensions
        ):
            return False
        if "code" in extensions and not _nonempty_string(extensions.get("code")):
            return False
    if "locations" in error:
        locations = error.get("locations")
        if not isinstance(locations, list) or not all(
            isinstance(location, Mapping)
            and _exact_positive_int(location.get("line"))
            and _exact_positive_int(location.get("column"))
            for location in locations
        ):
            return False
    return True


def _graphql_errors_schema_is_valid(payload: Mapping[str, Any]) -> bool:
    if "errors" not in payload:
        return True
    errors = payload.get("errors")
    return isinstance(errors, list) and all(
        _graphql_error_is_valid(error) for error in errors
    )


def _raise_for_graphql_errors(payload: Mapping[str, Any]) -> None:
    if not _graphql_errors_schema_is_valid(payload):
        raise GitHubSchemaError()
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return
    if any(
        error["type"] in {"FORBIDDEN", "NOT_FOUND", "UNAUTHORIZED"}
        for error in errors
    ):
        raise GitHubPermissionError()
    if any(error["type"] != "RATE_LIMITED" for error in errors):
        raise GitHubAcquisitionError()
    raise GitHubTransientError()


def _default_executor(
    argv: list[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@dataclass
class ReadOnlyGitHubCommandRunner:
    """Run only the exact, predeclared GraphQL query for one repository and PR."""

    executor: Executor = _default_executor
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.timeout_seconds > 120:
            raise ValueError("timeout_seconds must be greater than 0 and at most 120")

    @staticmethod
    def build_command(repository: str, pull_request: int) -> list[str]:
        if not _REPOSITORY.fullmatch(repository):
            raise UnsafeGitHubCommand("repository must be owner/name")
        if not isinstance(pull_request, int) or isinstance(pull_request, bool) or pull_request < 1:
            raise UnsafeGitHubCommand("pull request must be a positive integer")
        owner, name = repository.split("/", 1)
        return [
            "gh",
            "api",
            "graphql",
            "--hostname",
            "github.com",
            "--method",
            "POST",
            "--field",
            f"query={GRAPHQL_QUERY}",
            "--field",
            f"owner={owner}",
            "--field",
            f"name={name}",
            "--field",
            f"number={pull_request}",
        ]

    def run(
        self,
        argv: Sequence[str],
        *,
        repository: str,
        pull_request: int,
    ) -> Mapping[str, Any]:
        expected = self.build_command(repository, pull_request)
        if list(argv) != expected:
            raise UnsafeGitHubCommand(
                "only the fixed read-only, repository-and-PR-scoped query is allowed"
            )
        try:
            completed = self.executor(expected, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            raise GitHubTransientError() from None
        if completed.returncode != 0:
            try:
                error_payload = json.loads(completed.stdout)
            except (TypeError, json.JSONDecodeError):
                error_payload = None
            if isinstance(error_payload, Mapping):
                _raise_for_graphql_errors(error_payload)
            raise GitHubAcquisitionError()
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            raise GitHubMalformedResponseError() from None
        if not isinstance(payload, Mapping):
            raise GitHubMalformedResponseError()
        return payload


@dataclass(frozen=True)
class GitHubCliAdapter:
    runner: ReadOnlyGitHubCommandRunner

    def fetch(self, repository: str, pull_request: int) -> Mapping[str, Any]:
        command = self.runner.build_command(repository, pull_request)
        payload = self.runner.run(
            command, repository=repository, pull_request=pull_request
        )
        if not _graphql_errors_schema_is_valid(payload):
            raise GitHubSchemaError()
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            _raise_for_graphql_errors(payload)
            raise GitHubAcquisitionError()
        data = payload.get("data")
        repository_data = data.get("repository") if isinstance(data, Mapping) else None
        pr = repository_data.get("pullRequest") if isinstance(repository_data, Mapping) else None
        if not isinstance(data, Mapping) or not isinstance(repository_data, Mapping) or not isinstance(pr, Mapping):
            raise GitHubSchemaError()
        if _invalid_pr_scalar_fields(pr):
            raise GitHubSchemaError()
        if (
            "reviewDecision" not in pr
            or not _nullable_string(pr.get("reviewDecision"))
        ):
            raise GitHubSchemaError()
        if not isinstance(data.get("rateLimit"), Mapping):
            raise GitHubSchemaError()
        if not isinstance(repository_data.get("branchProtectionRules"), Mapping):
            raise GitHubSchemaError()
        if any(not isinstance(pr.get(key), Mapping) for key in ("labels", "reviewThreads", "reviews")):
            raise GitHubSchemaError()
        if "statusCheckRollup" not in pr:
            raise GitHubSchemaError()
        rollup = pr.get("statusCheckRollup")
        connections = [
            ("branch_protection", repository_data.get("branchProtectionRules")),
            ("labels", pr.get("labels")),
            ("review_threads", pr.get("reviewThreads")),
            ("reviews", pr.get("reviews")),
        ]
        if isinstance(rollup, Mapping):
            if not _nonempty_string(rollup.get("state")):
                raise GitHubSchemaError()
            connections.append(("checks", rollup.get("contexts")))
        elif rollup is not None:
            raise GitHubSchemaError()
        for name, connection in connections:
            if not _connection_schema_is_valid(name, connection):
                raise GitHubSchemaError()
        return payload

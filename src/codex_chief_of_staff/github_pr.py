"""Read-only GitHub acquisition for pull-request observations."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any


READ_ONLY_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      state
      headRefOid
      isDraft
      mergeable
      mergeStateStatus
      reviewDecision
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup {
              state
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    name status conclusion detailsUrl startedAt completedAt
                  }
                  ... on StatusContext { context state targetUrl createdAt }
                }
                pageInfo { hasNextPage }
              }
            }
          }
        }
      }
      reviews(first: 100) {
        nodes { author { login } state submittedAt commit { oid } }
        pageInfo { hasNextPage }
      }
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 1) { nodes { author { login } createdAt } }
        }
        pageInfo { hasNextPage }
      }
    }
  }
}
""".strip()


class ProviderError(RuntimeError):
    """A typed, bounded error from the GitHub acquisition boundary."""

    def __init__(self, kind: str, message: str, retryable: bool) -> None:
        self.kind = kind
        self.message = _bounded(message)
        self.retryable = retryable
        super().__init__(self.message)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "retryable": self.retryable,
        }


def _bounded(message: object, limit: int = 500) -> str:
    text = str(message).strip().replace("\x00", "")
    return text[:limit]


def classify_provider_error(message: object) -> ProviderError:
    bounded = _bounded(message)
    lowered = bounded.lower()
    structured_types = _graphql_error_types(message)
    if structured_types:
        if structured_types <= {"RATE_LIMITED"}:
            return ProviderError("rate-limit", bounded, True)
        if "FORBIDDEN" in structured_types:
            return ProviderError("permission", bounded, False)
        return ProviderError("graphql", bounded, False)
    if "rate limit" in lowered or "http 429" in lowered or "secondary rate" in lowered:
        return ProviderError("rate-limit", bounded, True)
    if (
        "resource not accessible" in lowered
        or "forbidden" in lowered
        or "http 403" in lowered
        or "permission" in lowered
    ):
        return ProviderError("permission", bounded, False)
    if (
        "graphql:" in lowered
        or "graphql error" in lowered
        or "doesn't exist on type" in lowered
        or "cannot query field" in lowered
        or "validation failed" in lowered
    ):
        return ProviderError("graphql", bounded, False)
    transient_markers = (
        "timed out",
        "timeout",
        "connection refused",
        "connection reset",
        "connection aborted",
        "could not resolve host",
        "network is unreachable",
        "temporary failure in name resolution",
        "tls handshake timeout",
    )
    if any(marker in lowered for marker in transient_markers):
        return ProviderError("transport", bounded, True)
    return ProviderError("github", bounded, False)


def _graphql_error_types(message: object) -> set[str]:
    """Extract trusted GraphQL error codes when gh returns structured JSON."""
    if not isinstance(message, str):
        return set()
    candidates = [message.strip()]
    first = message.find("{")
    last = message.rfind("}")
    if first >= 0 and last > first:
        candidates.append(message[first : last + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("errors"), list):
            continue
        types: set[str] = set()
        for raw in payload["errors"]:
            if not isinstance(raw, dict):
                types.add("INVALID_ERROR")
                continue
            error_type = raw.get("type")
            extensions = raw.get("extensions")
            if not isinstance(error_type, str) and isinstance(extensions, dict):
                error_type = extensions.get("code")
            types.add(
                error_type.upper() if isinstance(error_type, str) else "GRAPHQL_ERROR"
            )
        if types:
            return types
    return set()


class GitHubClient:
    """Fetch one PR through a fixed GraphQL query and the authenticated gh CLI."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout: float = 30.0,
    ) -> None:
        self._runner = runner
        self._timeout = timeout

    def fetch(self, repository: str, pr_number: int) -> dict[str, Any]:
        owner, name = validate_repository(repository)
        if isinstance(pr_number, bool) or pr_number < 1:
            raise ValueError("PR number must be a positive integer")
        args = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={READ_ONLY_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr_number}",
        ]
        try:
            completed = self._runner(
                args,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                "transport",
                f"GitHub observation timed out after {self._timeout:g}s",
                True,
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr or exc.stdout or f"gh exited with status {exc.returncode}"
            raise classify_provider_error(detail) from exc
        except OSError as exc:
            raise ProviderError("transport", str(exc), False) from exc

        if len(completed.stdout.encode("utf-8")) > 1_048_576:
            raise ProviderError("response", "GitHub response exceeded its byte limit", False)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError("response", "GitHub returned invalid JSON", False) from exc
        if not isinstance(payload, dict):
            raise ProviderError("response", "GitHub returned a non-object response", False)
        return payload


def validate_repository(repository: str) -> tuple[str, str]:
    parts = repository.split("/")
    if (
        len(parts) != 2
        or any(not part for part in parts)
        or any(len(part) > 100 for part in parts)
        or any(part in {".", ".."} for part in parts)
        or any(
            not all(character.isalnum() or character in ".-_" for character in part)
            for part in parts
        )
    ):
        raise ValueError("repository must use the owner/name form")
    return parts[0], parts[1]

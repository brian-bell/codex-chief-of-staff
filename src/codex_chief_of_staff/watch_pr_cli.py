"""Command-line entry point for the read-only GitHub PR watcher."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .github_pr import GitHubClient, validate_repository
from .pr_watcher import (
    RetryPolicy,
    normalize_timestamp,
    observe_with_retry,
    utc_now,
    validate_oid,
)


MAX_FIXTURE_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 131_072


class CliError(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.safe_message = message
        super().__init__(message)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliError("argument", "invalid command arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="watch-pr")
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    parser.add_argument("--pr", required=True, type=int, help="positive pull-request number")
    parser.add_argument(
        "--expected-head", help="head SHA to which existing verdict evidence is bound"
    )
    parser.add_argument(
        "--fixture", type=Path, help="read a saved GitHub response instead of the network"
    )
    parser.add_argument("--observed-at", help="fixed ISO-8601 observation time for fixture replay")
    parser.add_argument("--max-attempts", type=int, default=3, choices=range(1, 4))
    return parser


def json_line(value: object, *, limit: int = MAX_OUTPUT_BYTES) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    if len(encoded.encode("utf-8")) > limit:
        raise CliError("output", "watcher output exceeded its byte limit")
    return encoded


def _error_line(kind: str, message: str) -> str:
    return json_line(
        {"error": {"kind": kind, "message": message}},
        limit=1024,
    )


def _read_fixture(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as fixture:
            raw = fixture.read(MAX_FIXTURE_BYTES + 1)
    except OSError as exc:
        raise CliError("fixture", "fixture could not be read") from exc
    if len(raw) > MAX_FIXTURE_BYTES:
        raise CliError("fixture", "fixture exceeded its byte limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError("fixture", "fixture is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise CliError("fixture", "fixture must contain a JSON object")
    return payload


def _result_error_kind(result: dict[str, object]) -> str | None:
    provider_errors = result["provider_errors"]
    if provider_errors:
        kinds = {error["kind"] for error in provider_errors}
        if "permission" in kinds:
            return "permission"
        if "rate-limit" in kinds and kinds == {"rate-limit"}:
            return "rate-limit"
        if "transport" in kinds:
            return "transport"
        return "acquisition"
    if result["schema_errors"]:
        return "schema"
    return None


def run(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        try:
            validate_repository(args.repo)
        except ValueError as exc:
            raise CliError("argument", "repository must use bounded owner/name form") from exc
        if args.pr < 1:
            raise CliError("argument", "PR number must be a positive integer")
        if args.expected_head:
            if (
                len(args.expected_head) not in {40, 64}
                or any(
                    character not in "0123456789abcdefABCDEF"
                    for character in args.expected_head
                )
            ):
                raise CliError("argument", "expected head SHA is invalid")
            validate_oid(args.expected_head, "expected head SHA")
        if args.observed_at and not args.fixture:
            raise CliError("argument", "--observed-at requires --fixture")
        if args.observed_at:
            try:
                observed_at = normalize_timestamp(args.observed_at)
            except ValueError as exc:
                raise CliError("argument", "--observed-at is invalid") from exc
        else:
            observed_at = None
        if args.fixture:
            payload = _read_fixture(args.fixture)

            def fetch() -> dict[str, object]:
                return payload
        else:
            client = GitHubClient(timeout=30.0)
            fetch = lambda: client.fetch(args.repo, args.pr)
        delays = tuple(1.0 for _ in range(args.max_attempts - 1))
        clock = (lambda: str(observed_at)) if observed_at else utc_now
        result = observe_with_retry(
            fetch,
            args.repo,
            args.pr,
            expected_head_sha=args.expected_head,
            policy=RetryPolicy(max_attempts=args.max_attempts, delays=delays),
            clock=clock,
        )
        error_kind = _result_error_kind(result)
        if error_kind:
            raise CliError(error_kind, "GitHub evidence could not be classified safely")
        output = json_line(result)
    except CliError as exc:
        print(_error_line(exc.kind, exc.safe_message), file=sys.stderr, end="")
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            _error_line("internal", "watcher failed without exposing provider data"),
            file=sys.stderr,
            end="",
        )
        return 2
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

"""JSON command-line interface for the read-only GitHub PR watcher."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from .github_pr import (
    GitHubAcquisitionError,
    GitHubCliAdapter,
    GitHubTransientError,
    ReadOnlyGitHubCommandRunner,
    UnsafeGitHubCommand,
)
from .pr_watcher import RetryPolicy, classify_github_response


class CliError(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliError("usage", message)


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="watch-pr", add_help=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--expected-head")
    parser.add_argument("--verdict-head")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--observed-at")
    parser.add_argument("--max-retries", type=int, default=2)
    return parser


def _clock(value: str | None):
    if value is None:
        return lambda: datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CliError("input", "--observed-at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CliError("input", "--observed-at must include a timezone")
    return lambda: parsed


def _write_json(stream, value: object) -> None:
    stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def run(
    argv: list[str] | None = None,
    *,
    adapter: GitHubCliAdapter | None = None,
    sleeper=time.sleep,
) -> int:
    try:
        args = _parser().parse_args(argv)
        ReadOnlyGitHubCommandRunner.build_command(args.repo, args.pr)
        clock = _clock(args.observed_at)
        policy = RetryPolicy(
            max_retries=args.max_retries,
            backoff_seconds=(1.0, 2.0, 4.0, 8.0, 16.0),
        )
        if args.fixture:
            try:
                payload = json.loads(args.fixture.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise CliError("input", "fixture is not readable JSON") from exc
            result = classify_github_response(
                args.repo,
                args.pr,
                payload,
                expected_head=args.expected_head,
                verdict_head=args.verdict_head,
                clock=clock,
            )
        else:
            live_adapter = adapter or GitHubCliAdapter(ReadOnlyGitHubCommandRunner())
            delays: list[float] = []
            for attempt in range(1, policy.max_retries + 2):
                try:
                    payload = live_adapter.fetch(args.repo, args.pr)
                    break
                except GitHubTransientError:
                    if attempt > policy.max_retries:
                        raise
                    delay = policy.backoff_seconds[attempt - 1]
                    delays.append(delay)
                    sleeper(delay)
            result = classify_github_response(
                args.repo,
                args.pr,
                payload,
                expected_head=args.expected_head,
                verdict_head=args.verdict_head,
                clock=clock,
                retry_evidence={
                    "attempts": attempt,
                    "delays_seconds": delays,
                    "exhausted": False,
                },
            )
        _write_json(sys.stdout, result)
        return 0
    except CliError as exc:
        _write_json(sys.stderr, {"error": {"message": str(exc), "type": exc.kind}})
        return 2
    except (UnsafeGitHubCommand, ValueError) as exc:
        _write_json(sys.stderr, {"error": {"message": str(exc), "type": "input"}})
        return 2
    except GitHubAcquisitionError as exc:
        _write_json(
            sys.stderr,
            {"error": {"message": exc.safe_message, "type": exc.kind}},
        )
        return exc.exit_code
    except Exception:
        _write_json(
            sys.stderr,
            {"error": {"message": "watcher failed without exposing response content", "type": "runtime"}},
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run())

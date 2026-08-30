from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_chief_of_staff.github_pr import (
    GitHubAcquisitionError,
    GitHubCliAdapter,
    GitHubMalformedResponseError,
    GitHubPermissionError,
    GitHubSchemaError,
    GitHubTransientError,
    ReadOnlyGitHubCommandRunner,
    UnsafeGitHubCommand,
)
from codex_chief_of_staff.watch_pr_cli import run as run_watch_cli


CLI = ROOT / "skills" / "babysit-pr" / "scripts" / "watch-pr"
FIXTURE = ROOT / "tests" / "fixtures" / "github-pr" / "merge-ready.json"


class GitHubAdapterTest(unittest.TestCase):
    def test_runner_accepts_only_the_fixed_repo_and_pr_scoped_query(self) -> None:
        calls = []

        def execute(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            self.assertGreater(timeout, 0)
            return subprocess.CompletedProcess(argv, 0, FIXTURE.read_text(), "")

        runner = ReadOnlyGitHubCommandRunner(executor=execute)
        command = runner.build_command("example/project", 17)
        result = runner.run(command, repository="example/project", pull_request=17)

        self.assertIn("repository(owner:$owner,name:$name)", " ".join(command))
        self.assertIn("pullRequest(number:$number)", " ".join(command))
        self.assertIn("owner=example", command)
        self.assertIn("name=project", command)
        self.assertIn("number=17", command)
        self.assertNotIn("mutation", " ".join(command).lower())
        self.assertNotIn(" title ", f" {' '.join(command).lower()} ")
        self.assertNotIn(" summary ", f" {' '.join(command).lower()} ")
        self.assertNotIn(" body ", f" {' '.join(command).lower()} ")
        self.assertEqual(result["data"]["repository"]["pullRequest"]["state"], "OPEN")
        self.assertEqual(len(calls), 1)

        rejected = (
            ["gh", "pr", "merge", "17", "--repo", "example/project"],
            [*command[:-1], "number=18"],
            [*command, "--field", "query=mutation { addComment(input:{}) { clientMutationId } }"],
            runner.build_command("other/project", 17),
        )
        for unsafe in rejected:
            with self.subTest(command=unsafe):
                with self.assertRaises(UnsafeGitHubCommand):
                    runner.run(
                        list(unsafe), repository="example/project", pull_request=17
                    )
        self.assertEqual(len(calls), 1)

    def test_adapter_uses_only_the_read_only_runner(self) -> None:
        calls = []
        runner = ReadOnlyGitHubCommandRunner(
            executor=lambda argv, timeout: calls.append(argv)
            or subprocess.CompletedProcess(argv, 0, FIXTURE.read_text(), "")
        )
        payload = GitHubCliAdapter(runner).fetch("example/project", 17)
        self.assertEqual(payload["data"]["repository"]["pullRequest"]["headRefOid"], "a" * 40)
        self.assertEqual(len(calls), 1)

    def test_adapter_accepts_null_status_check_rollup(self) -> None:
        payload = json.loads(FIXTURE.read_text())
        payload["data"]["repository"]["pullRequest"]["statusCheckRollup"] = None
        adapter = GitHubCliAdapter(
            ReadOnlyGitHubCommandRunner(
                executor=lambda argv, timeout: subprocess.CompletedProcess(
                    argv, 0, json.dumps(payload), ""
                )
            )
        )

        result = adapter.fetch("example/project", 17)

        self.assertIsNone(
            result["data"]["repository"]["pullRequest"]["statusCheckRollup"]
        )

    def test_transport_timeout_is_explicit_and_retries_without_off_by_one(self) -> None:
        calls = []

        def timeout(argv, *, timeout):
            calls.append((argv, timeout))
            raise subprocess.TimeoutExpired(argv, timeout)

        runner = ReadOnlyGitHubCommandRunner(executor=timeout, timeout_seconds=7.5)
        adapter = GitHubCliAdapter(runner)
        stdout = StringIO()
        stderr = StringIO()
        delays = []
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = run_watch_cli(
                ["--repo", "example/project", "--pr", "17", "--max-retries", "2"],
                adapter=adapter,
                sleeper=delays.append,
            )
        self.assertEqual(exit_code, 6)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["error"]["type"], "transient-exhausted")
        self.assertEqual(len(calls), 3)
        self.assertEqual([value for _argv, value in calls], [7.5, 7.5, 7.5])
        self.assertEqual(delays, [1.0, 2.0])

    def test_nonzero_graphql_rate_limit_and_permission_errors_are_typed(self) -> None:
        cases = (
            (
                {"errors": [{"type": "RATE_LIMITED", "message": "secret token"}]},
                6,
                "transient-exhausted",
                3,
                [1.0, 2.0],
            ),
            (
                {"errors": [{"type": "FORBIDDEN", "message": "secret token"}]},
                4,
                "permission",
                1,
                [],
            ),
        )
        for payload, expected_exit, expected_type, expected_calls, expected_delays in cases:
            with self.subTest(error_type=expected_type):
                calls = []

                def execute(argv, *, timeout):
                    calls.append((argv, timeout))
                    return subprocess.CompletedProcess(
                        argv, 1, json.dumps(payload), "secret stderr token"
                    )

                adapter = GitHubCliAdapter(
                    ReadOnlyGitHubCommandRunner(executor=execute, timeout_seconds=4.0)
                )
                stdout = StringIO()
                stderr = StringIO()
                delays = []
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = run_watch_cli(
                        ["--repo", "example/project", "--pr", "17", "--max-retries", "2"],
                        adapter=adapter,
                        sleeper=delays.append,
                    )
                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(json.loads(stderr.getvalue())["error"]["type"], expected_type)
                self.assertNotIn("secret", stderr.getvalue())
                self.assertEqual(len(calls), expected_calls)
                self.assertEqual([timeout for _argv, timeout in calls], [4.0] * expected_calls)
                self.assertEqual(delays, expected_delays)

    def test_mixed_graphql_errors_never_retry_and_ignore_source_order(self) -> None:
        cases = (
            (
                [
                    {"path": ["repository", 0], "type": "RATE_LIMITED"},
                    {"path": ["repository", 1], "type": "FORBIDDEN"},
                ],
                4,
                "permission",
            ),
            (
                [
                    {"path": ["repository", 0], "type": "RATE_LIMITED"},
                    {"path": ["repository", 1], "type": "INTERNAL"},
                ],
                3,
                "acquisition",
            ),
        )
        for errors, expected_exit, expected_type in cases:
            for ordered in (errors, list(reversed(errors))):
                with self.subTest(error_type=expected_type, reversed=ordered != errors):
                    calls = []

                    def execute(argv, *, timeout):
                        calls.append((argv, timeout))
                        return subprocess.CompletedProcess(
                            argv, 1, json.dumps({"errors": ordered}), "secret stderr"
                        )

                    adapter = GitHubCliAdapter(
                        ReadOnlyGitHubCommandRunner(executor=execute, timeout_seconds=4.0)
                    )
                    stdout = StringIO()
                    stderr = StringIO()
                    delays = []
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = run_watch_cli(
                            ["--repo", "example/project", "--pr", "17", "--max-retries", "2"],
                            adapter=adapter,
                            sleeper=delays.append,
                        )
                    self.assertEqual(exit_code, expected_exit)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(json.loads(stderr.getvalue())["error"]["type"], expected_type)
                    self.assertNotIn("secret", stderr.getvalue())
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(delays, [])

    def test_oversized_live_error_evidence_stays_compact_and_typed(self) -> None:
        oversized = "secret-prefix-" + ("x" * 1_000_000) + "-secret-suffix"
        oversized_integer = int("9" * 1_000)
        payload = {
            "errors": [
                {
                    "extensions": {"detail": oversized},
                    "locations": [{"column": 2, "line": 1} for _ in range(1_000)],
                    "message": oversized,
                    "path": [oversized, oversized_integer],
                    "type": "RATE_LIMITED",
                }
            ]
        }
        calls = []

        def execute(argv, *, timeout):
            calls.append((argv, timeout))
            return subprocess.CompletedProcess(
                argv, 1, json.dumps(payload), "secret stderr"
            )

        adapter = GitHubCliAdapter(ReadOnlyGitHubCommandRunner(executor=execute))
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = run_watch_cli(
                ["--repo", "example/project", "--pr", "17", "--max-retries", "0"],
                adapter=adapter,
                sleeper=lambda _delay: None,
            )
        self.assertEqual(exit_code, 6)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["error"]["type"], "transient-exhausted")
        self.assertLess(len(stderr.getvalue()), 500)
        self.assertNotIn("secret-prefix", stderr.getvalue())
        self.assertNotIn("secret-suffix", stderr.getvalue())
        self.assertNotIn("9" * 81, stderr.getvalue())
        self.assertEqual(len(calls), 1)


class WatchPrCliTest(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_fixture_cli_writes_one_compact_sorted_json_value(self) -> None:
        completed = self.run_cli(
            "--repo", "example/project",
            "--pr", "17",
            "--fixture", str(FIXTURE),
            "--observed-at", "2026-08-29T12:00:00Z",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["classification"], "merge-ready")
        self.assertEqual(
            completed.stdout,
            json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
        )
        self.assertEqual(completed.stderr, "")

    def test_fixture_cli_bounds_oversized_provider_error_evidence(self) -> None:
        oversized = "secret-prefix-" + ("x" * 1_000_000) + "-secret-suffix"
        payload = json.loads(FIXTURE.read_text())
        payload["errors"] = [
            {
                "extensions": {"detail": oversized},
                "locations": [{"column": 2, "line": 1} for _ in range(1_000)],
                "message": oversized,
                "path": [oversized],
                "type": "RATE_LIMITED",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "oversized.json"
            fixture.write_text(json.dumps(payload))
            completed = self.run_cli(
                "--repo", "example/project",
                "--pr", "17",
                "--fixture", str(fixture),
                "--observed-at", "2026-08-29T12:00:00Z",
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["classification"], "plausible-transient-failure")
        self.assertLess(len(completed.stdout), 10_000)
        self.assertLessEqual(len(result["partial_response"]["errors"][0]["path"][0]), 80)
        self.assertNotIn("secret-suffix", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_fixture_cli_canonically_bounds_integer_error_paths(self) -> None:
        integers = [int("7" * 79), int("8" * 80), int("9" * 81), int("6" * 1_000)]
        payload = json.loads(FIXTURE.read_text())
        payload["errors"] = [
            {"path": ["repository", value], "type": "RATE_LIMITED"}
            for value in reversed(integers)
        ]
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "integer-paths.json"
            fixture.write_text(json.dumps(payload))
            completed = self.run_cli(
                "--repo", "example/project",
                "--pr", "17",
                "--fixture", str(fixture),
                "--observed-at", "2026-08-29T12:00:00Z",
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["classification"], "plausible-transient-failure")
        numeric_parts = [error["path"][1] for error in result["partial_response"]["errors"]]
        self.assertTrue(all(len(part) <= 80 for part in numeric_parts))
        self.assertIn("7" * 79, numeric_parts)
        self.assertIn("8" * 80, numeric_parts)
        self.assertEqual(sum(part.startswith("<int:") for part in numeric_parts), 2)
        self.assertTrue(
            all(
                len(part) == 77
                for part in numeric_parts
                if part.startswith("<int:")
            )
        )
        self.assertTrue(
            all(
                part.startswith("<int:sha256:")
                for part in numeric_parts
                if part.startswith("<int:")
            )
        )
        self.assertLess(len(completed.stdout), 10_000)
        self.assertNotIn("6" * 81, completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_usage_and_input_errors_are_compact_json_on_stderr(self) -> None:
        for args in (("--repo", "invalid", "--pr", "17"), ("--repo", "example/project",)):
            with self.subTest(args=args):
                completed = self.run_cli(*args)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, "")
                error = json.loads(completed.stderr)
                self.assertIn(error["error"]["type"], {"input", "usage"})
                self.assertEqual(
                    completed.stderr,
                    json.dumps(error, sort_keys=True, separators=(",", ":")) + "\n",
                )

    def test_live_acquisition_errors_have_nonzero_safe_stderr_contracts(self) -> None:
        class FailingAdapter:
            def __init__(self, error):
                self.error = error

            def fetch(self, _repository, _pull_request):
                raise self.error

        cases = (
            (GitHubAcquisitionError(), 3, "acquisition"),
            (GitHubPermissionError(), 4, "permission"),
            (GitHubMalformedResponseError(), 5, "malformed-response"),
            (GitHubSchemaError(), 5, "schema"),
            (GitHubTransientError(), 6, "transient-exhausted"),
        )
        for error, exit_code, kind in cases:
            with self.subTest(kind=kind):
                stdout = StringIO()
                stderr = StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    actual = run_watch_cli(
                        ["--repo", "example/project", "--pr", "17", "--max-retries", "0"],
                        adapter=FailingAdapter(error),
                        sleeper=lambda _delay: None,
                    )
                self.assertEqual(actual, exit_code)
                self.assertEqual(stdout.getvalue(), "")
                value = json.loads(stderr.getvalue())
                self.assertEqual(value["error"]["type"], kind)
                self.assertNotIn("token", stderr.getvalue().lower())
                self.assertEqual(
                    stderr.getvalue(),
                    json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
                )

    def test_adapter_rejects_graphql_errors_and_required_schema_failures(self) -> None:
        def adapter_for(payload):
            return GitHubCliAdapter(
                ReadOnlyGitHubCommandRunner(
                    executor=lambda argv, timeout: subprocess.CompletedProcess(
                        argv, 0, json.dumps(payload), "secret stderr token"
                    )
                )
            )

        with self.assertRaises(GitHubPermissionError):
            adapter_for(
                {
                    "data": {"repository": {"pullRequest": None}},
                    "errors": [{"type": "FORBIDDEN", "message": "secret token"}],
                }
            ).fetch("example/project", 17)
        with self.assertRaises(GitHubTransientError):
            adapter_for(
                {
                    "data": {"repository": {"pullRequest": None}},
                    "errors": [{"type": "RATE_LIMITED", "message": "secret token"}],
                }
            ).fetch("example/project", 17)
        with self.assertRaises(GitHubSchemaError):
            adapter_for({"data": {"repository": {"pullRequest": {}}}}).fetch(
                "example/project", 17
            )
        with self.assertRaises(GitHubMalformedResponseError):
            adapter_for(["not", "an", "object"]).fetch("example/project", 17)
        missing_page = json.loads(FIXTURE.read_text())
        del missing_page["data"]["repository"]["pullRequest"]["labels"]["pageInfo"]
        with self.assertRaises(GitHubSchemaError):
            adapter_for(missing_page).fetch("example/project", 17)

        def connections(payload):
            repository = payload["data"]["repository"]
            pr = repository["pullRequest"]
            return (
                repository["branchProtectionRules"],
                pr["labels"],
                pr["reviewThreads"],
                pr["reviews"],
                pr["statusCheckRollup"]["contexts"],
            )

        for index in range(5):
            for malformed in ("too-small", "negative", "too-many", "non-object"):
                with self.subTest(connection=index, malformed=malformed):
                    payload = json.loads(FIXTURE.read_text())
                    connection = connections(payload)[index]
                    if malformed == "too-small":
                        connection["totalCount"] = len(connection["nodes"]) - 1
                    elif malformed == "negative":
                        connection["nodes"] = []
                        connection["totalCount"] = -1
                    elif malformed == "too-many":
                        exemplar = connection["nodes"][0] if connection["nodes"] else {}
                        connection["nodes"] = [exemplar for _ in range(101)]
                        connection["totalCount"] = 101
                    else:
                        connection["nodes"] = ["not-an-object"]
                        connection["totalCount"] = 1
                    with self.assertRaises(GitHubSchemaError):
                        adapter_for(payload).fetch("example/project", 17)

    def test_adapter_rejects_typed_node_and_scalar_schema_failures(self) -> None:
        def fetch(payload):
            adapter = GitHubCliAdapter(
                ReadOnlyGitHubCommandRunner(
                    executor=lambda argv, timeout: subprocess.CompletedProcess(
                        argv, 0, json.dumps(payload), ""
                    )
                )
            )
            return adapter.fetch("example/project", 17)

        for location in (
            lambda payload: payload["data"]["repository"]["branchProtectionRules"],
            lambda payload: payload["data"]["repository"]["pullRequest"]["labels"],
            lambda payload: payload["data"]["repository"]["pullRequest"]["reviewThreads"],
            lambda payload: payload["data"]["repository"]["pullRequest"]["reviews"],
            lambda payload: payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"],
        ):
            payload = json.loads(FIXTURE.read_text())
            connection = location(payload)
            connection["nodes"] = [{}]
            connection["totalCount"] = 1
            with self.assertRaises(GitHubSchemaError):
                fetch(payload)

        invalid_scalars = {
            "baseRefName": "",
            "headRefOid": "not-a-sha",
            "isDraft": 0,
            "mergeStateStatus": False,
            "mergeable": [],
            "state": {},
            "reviewDecision": 1,
        }
        for field, value in invalid_scalars.items():
            with self.subTest(field=field):
                payload = json.loads(FIXTURE.read_text())
                payload["data"]["repository"]["pullRequest"][field] = value
                with self.assertRaises(GitHubSchemaError):
                    fetch(payload)

        payload = json.loads(FIXTURE.read_text())
        payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]["state"] = False
        with self.assertRaises(GitHubSchemaError):
            fetch(payload)

    def test_adapter_rejects_malformed_graphql_error_shapes(self) -> None:
        malformed_errors = (
            None,
            {},
            "RATE_LIMITED",
            7,
            [None],
            [{}],
            [{"path": []}],
            [{"type": False}],
            [{"type": "RATE_LIMITED", "path": "repository"}],
            [{"type": "RATE_LIMITED", "path": [-1]}],
            [{"type": "RATE_LIMITED", "path": [True]}],
            [{"type": "RATE_LIMITED", "extensions": []}],
            [{"type": "RATE_LIMITED", "locations": [{"line": 1, "column": True}]}],
        )

        def adapter_for(payload, returncode=0):
            return GitHubCliAdapter(
                ReadOnlyGitHubCommandRunner(
                    executor=lambda argv, timeout: subprocess.CompletedProcess(
                        argv, returncode, json.dumps(payload), "secret stderr"
                    )
                )
            )

        for errors in malformed_errors:
            with self.subTest(errors=errors):
                payload = json.loads(FIXTURE.read_text())
                payload["errors"] = errors
                with self.assertRaises(GitHubSchemaError):
                    adapter_for(payload).fetch("example/project", 17)

        payload = {"errors": [{"type": "RATE_LIMITED", "extensions": "bad"}]}
        with self.assertRaises(GitHubSchemaError):
            adapter_for(payload, returncode=1).fetch("example/project", 17)


if __name__ == "__main__":
    unittest.main()

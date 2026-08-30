from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "skills" / "chief-of-staff" / "scripts" / "chief-of-staff-state"


class StateCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db = Path(self.tempdir.name) / "state.db"

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), "--db", str(self.db), *args],
            cwd=ROOT,
            check=check,
            capture_output=True,
            text=True,
        )

    def record_worker(self, work_id: str, task_id: str) -> None:
        self.run_cli(
            "dispatch", "record", work_id, "--task-id", task_id,
            "--role", "worker", "--environment", "worktree",
            "--brief-digest", f"sha256:{work_id.lower()}",
        )

    def test_create_and_show_build_work_order(self) -> None:
        self.run_cli("init")
        created = self.run_cli(
            "work",
            "create",
            "--id",
            "CS-104",
            "--title",
            "Fix password reset persistence",
            "--mode",
            "build",
            "--authority",
            "local-write",
            "--coordinator-task-id",
            "chief-task",
        )

        self.assertEqual(json.loads(created.stdout)["status"], "draft")
        shown = json.loads(self.run_cli("work", "show", "CS-104").stdout)
        self.assertEqual(shown["id"], "CS-104")
        self.assertEqual(shown["mode"], "build")
        self.assertEqual(shown["authority"], "local-write")

    def test_invalid_transition_fails_without_changing_status(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work",
            "create",
            "--id",
            "CS-105",
            "--title",
            "Skip the queue",
            "--mode",
            "build",
            "--authority",
            "local-write",
        )

        failed = self.run_cli(
            "work",
            "transition",
            "CS-105",
            "--to",
            "published",
            "--evidence",
            '{"pull_request":"https://example.test/pr/1"}',
            check=False,
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("invalid transition", failed.stderr)
        shown = json.loads(self.run_cli("work", "show", "CS-105").stdout)
        self.assertEqual(shown["status"], "draft")

    def test_build_authority_cannot_publish(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work",
            "create",
            "--id",
            "CS-106",
            "--title",
            "Build only",
            "--mode",
            "build",
            "--authority",
            "local-write",
            "--coordinator-task-id",
            "chief-task",
        )
        self.record_worker("CS-106", "worker-106")
        for target in ("queued", "dispatched", "running", "ready-to-publish"):
            self.run_cli(
                "work",
                "transition",
                "CS-106",
                "--to",
                target,
                "--evidence",
                json.dumps({"observed": target}),
            )

        failed = self.run_cli(
            "work",
            "transition",
            "CS-106",
            "--to",
            "published",
            "--evidence",
            '{"pull_request":"https://example.test/pr/2","head_sha":"abc123"}',
            check=False,
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("requires publish authority", failed.stderr)

    def test_merge_authority_does_not_imply_publish_authority(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work", "create", "--id", "CS-106B", "--title", "Merge is separate",
            "--mode", "land", "--authority", "merge",
            "--coordinator-task-id", "chief-task",
        )
        self.record_worker("CS-106B", "worker-106b")
        for target in ("queued", "dispatched", "running", "ready-to-publish"):
            self.run_cli(
                "work", "transition", "CS-106B", "--to", target,
                "--evidence", json.dumps({"observed": target}),
            )

        failed = self.run_cli(
            "work", "transition", "CS-106B", "--to", "published",
            "--evidence", '{"pull_request":"https://example.test/pr/land","head_sha":"abc999"}',
            check=False,
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("requires publish authority", failed.stderr)

    def test_explicit_promotion_allows_publish_and_records_artifact(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work", "create", "--id", "CS-107", "--title", "Publish me",
            "--mode", "build", "--authority", "local-write",
            "--coordinator-task-id", "chief-task",
        )
        self.record_worker("CS-107", "worker-107")
        for target in ("queued", "dispatched", "running", "ready-to-publish"):
            self.run_cli(
                "work", "transition", "CS-107", "--to", target,
                "--evidence", json.dumps({"observed": target}),
            )

        self.run_cli(
            "work", "promote", "CS-107", "--mode", "publish",
            "--authority", "publish", "--evidence", '{"user_instruction":"Open the PR"}',
        )
        shown = json.loads(
            self.run_cli(
                "work", "transition", "CS-107", "--to", "published",
                "--evidence",
                '{"pull_request":"https://example.test/pr/3","head_sha":"abc123"}',
            ).stdout
        )

        self.assertEqual(shown["status"], "published")
        self.assertEqual(shown["mode"], "publish")
        self.assertEqual(shown["pull_request"], "https://example.test/pr/3")
        self.assertEqual(shown["head_sha"], "abc123")

    def test_dispatch_recording_is_idempotent_and_rejects_a_second_worker(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work", "create", "--id", "CS-108", "--title", "One writer",
            "--mode", "build", "--authority", "local-write",
            "--coordinator-task-id", "chief-task",
        )
        args = (
            "dispatch", "record", "CS-108", "--task-id", "task-1",
            "--role", "worker", "--environment", "worktree",
            "--brief-digest", "sha256:brief",
        )

        first = json.loads(self.run_cli(*args).stdout)
        second = json.loads(self.run_cli(*args).stdout)
        conflict = self.run_cli(
            "dispatch", "record", "CS-108", "--task-id", "task-2",
            "--role", "worker", "--environment", "worktree",
            "--brief-digest", "sha256:brief", check=False,
        )

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["callback"]["task_id"], "chief-task")
        self.assertEqual(first["callback"]["work_id"], "CS-108")
        self.assertIn("done", first["callback"]["statuses"])
        self.assertIn("needs-attention", first["callback"]["statuses"])
        events = json.loads(self.run_cli("event", "list", "CS-108").stdout)
        dispatch_event = next(
            event for event in events if event["event_type"] == "task-dispatched"
        )
        self.assertEqual(
            dispatch_event["payload"]["callback"]["task_id"], "chief-task"
        )
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("role already linked", conflict.stderr)

    def test_dispatch_requires_a_coordinator_callback_target(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work", "create", "--id", "CS-108B", "--title", "Call the chief back",
            "--mode", "build", "--authority", "local-write",
        )

        failed = self.run_cli(
            "dispatch", "record", "CS-108B", "--task-id", "task-callback",
            "--role", "worker", "--environment", "worktree",
            "--brief-digest", "sha256:callback", check=False,
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("coordinator task ID", failed.stderr)

    def test_new_head_sha_invalidates_old_verdict(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work", "create", "--id", "CS-109", "--title", "Review exact head",
            "--mode", "review", "--authority", "read-only",
        )
        self.run_cli(
            "work", "set-head", "CS-109", "--head-sha", "abc123",
            "--evidence", '{"source":"worker-report"}',
        )
        self.run_cli(
            "verdict", "record", "CS-109", "--id", "verdict-1",
            "--head-sha", "abc123", "--verdict", "pass", "--risk", "medium",
            "--reviewer-task-id", "review-task-1", "--evidence", '{"tests":["unit"]}',
        )
        current = json.loads(self.run_cli("verdict", "current", "CS-109").stdout)
        self.assertTrue(current["applicable"])

        self.run_cli(
            "work", "set-head", "CS-109", "--head-sha", "def456",
            "--evidence", '{"source":"fix-forward"}',
        )
        stale = json.loads(self.run_cli("verdict", "current", "CS-109").stdout)

        self.assertFalse(stale["applicable"])
        self.assertEqual(stale["head_sha"], "def456")
        self.assertEqual(stale["stale_verdicts"][0]["head_sha"], "abc123")

    def test_dispatched_status_requires_a_recorded_task(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work", "create", "--id", "CS-110", "--title", "No phantom task",
            "--mode", "build", "--authority", "local-write",
        )
        self.run_cli(
            "work", "transition", "CS-110", "--to", "queued",
            "--evidence", '{"reason":"ready"}',
        )

        failed = self.run_cli(
            "work", "transition", "CS-110", "--to", "dispatched",
            "--evidence", '{"claimed_task_id":"missing"}', check=False,
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("requires a recorded task link", failed.stderr)

    def test_open_gate_blocks_advancement_until_resolved(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work", "create", "--id", "CS-111", "--title", "Needs product choice",
            "--mode", "build", "--authority", "local-write",
            "--coordinator-task-id", "chief-task",
        )
        self.record_worker("CS-111", "worker-111")
        for target in ("queued", "dispatched", "running"):
            self.run_cli(
                "work", "transition", "CS-111", "--to", target,
                "--evidence", json.dumps({"observed": target}),
            )
        self.run_cli(
            "gate", "open", "CS-111", "--id", "gate-1",
            "--question", "Should the old default remain compatible?",
        )

        blocked = self.run_cli(
            "work", "transition", "CS-111", "--to", "ready-to-publish",
            "--evidence", '{"tests":"pass"}', check=False,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("open gates", blocked.stderr)

        self.run_cli(
            "gate", "resolve", "gate-1", "--answer", "Keep the old default",
        )
        ready = json.loads(
            self.run_cli(
                "work", "transition", "CS-111", "--to", "ready-to-publish",
                "--evidence", '{"tests":"pass"}',
            ).stdout
        )
        self.assertEqual(ready["status"], "ready-to-publish")

    def test_landing_requires_a_current_passing_verdict(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work", "create", "--id", "CS-112", "--title", "Land safely",
            "--mode", "build", "--authority", "local-write",
            "--coordinator-task-id", "chief-task",
        )
        self.record_worker("CS-112", "worker-112")
        for target in ("queued", "dispatched", "running", "ready-to-publish"):
            self.run_cli(
                "work", "transition", "CS-112", "--to", target,
                "--evidence", json.dumps({"observed": target}),
            )
        self.run_cli(
            "work", "promote", "CS-112", "--mode", "publish", "--authority", "publish",
            "--evidence", '{"user_instruction":"Publish it"}',
        )
        self.run_cli(
            "work", "transition", "CS-112", "--to", "published", "--evidence",
            '{"pull_request":"https://example.test/pr/4","head_sha":"aaa111"}',
        )
        self.run_cli(
            "work", "transition", "CS-112", "--to", "merge-ready",
            "--evidence", '{"checks":"green","merge_state":"clean"}',
        )
        self.run_cli(
            "work", "promote", "CS-112", "--mode", "land", "--authority", "merge",
            "--evidence", '{"user_instruction":"Land it"}',
        )

        blocked = self.run_cli(
            "work", "transition", "CS-112", "--to", "landing",
            "--evidence", '{"merge_method":"squash"}', check=False,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("current passing verdict", blocked.stderr)

        self.run_cli(
            "verdict", "record", "CS-112", "--id", "verdict-land",
            "--head-sha", "aaa111", "--verdict", "pass", "--risk", "high",
            "--reviewer-task-id", "review-112", "--evidence", '{"review":"complete"}',
        )
        self.run_cli(
            "verdict", "record", "CS-112", "--id", "verdict-land-fail",
            "--head-sha", "aaa111", "--verdict", "fail", "--risk", "high",
            "--reviewer-task-id", "review-113", "--evidence", '{"finding":"regression"}',
        )
        failed_latest = self.run_cli(
            "work", "transition", "CS-112", "--to", "landing",
            "--evidence", '{"merge_method":"squash"}', check=False,
        )
        self.assertNotEqual(failed_latest.returncode, 0)
        self.assertIn("latest verdict", failed_latest.stderr)

        self.run_cli(
            "verdict", "record", "CS-112", "--id", "verdict-land-final",
            "--head-sha", "aaa111", "--verdict", "pass-with-notes", "--risk", "high",
            "--reviewer-task-id", "review-114", "--evidence", '{"review":"fixed"}',
        )
        landing = json.loads(
            self.run_cli(
                "work", "transition", "CS-112", "--to", "landing",
                "--evidence", '{"merge_method":"squash"}',
            ).stdout
        )
        self.assertEqual(landing["status"], "landing")

    def test_event_append_is_idempotent_by_key(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work", "create", "--id", "CS-113", "--title", "Recover events",
            "--mode", "scout", "--authority", "read-only",
        )
        args = (
            "event", "append", "CS-113", "--type", "worker-status",
            "--payload", '{"status":"running"}', "--idempotency-key", "evt-1",
        )

        first = json.loads(self.run_cli(*args).stdout)
        second = json.loads(self.run_cli(*args).stdout)
        conflict = self.run_cli(
            "event", "append", "CS-113", "--type", "worker-status",
            "--payload", '{"status":"failed"}', "--idempotency-key", "evt-1",
            check=False,
        )

        self.assertEqual(first["sequence"], second["sequence"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("idempotency key conflict", conflict.stderr)

    def test_open_work_view_restores_project_context(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "project", "put", "--id", "project-1", "--name", "Accounts",
            "--codex-project-id", "codex-project-1", "--repository", "acme/accounts",
            "--source-control", "github", "--default-branch", "main",
        )
        self.run_cli(
            "work", "create", "--id", "CS-114", "--title", "Resume me",
            "--mode", "scout", "--authority", "read-only",
            "--project-id", "project-1", "--coordinator-task-id", "chief-task",
        )
        self.run_cli(
            "dispatch", "record", "CS-114", "--task-id", "scout-task-114",
            "--role", "scout", "--environment", "local",
            "--brief-digest", "sha256:cs-114",
        )

        restored = json.loads(self.run_cli("work", "list", "--open").stdout)

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["id"], "CS-114")
        self.assertEqual(restored[0]["project"]["repository"], "acme/accounts")
        self.assertEqual(restored[0]["coordinator_task_id"], "chief-task")
        self.assertEqual(restored[0]["tasks"][0]["task_id"], "scout-task-114")
        self.assertEqual(restored[0]["tasks"][0]["role"], "scout")

    def test_failed_audit_insert_rolls_back_state_transition(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work", "create", "--id", "CS-115", "--title", "Atomic state",
            "--mode", "scout", "--authority", "read-only",
        )
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_transition_event
                BEFORE INSERT ON events
                WHEN NEW.event_type = 'state-transition'
                BEGIN
                    SELECT RAISE(ABORT, 'forced event failure');
                END
                """
            )

        failed = self.run_cli(
            "work", "transition", "CS-115", "--to", "queued",
            "--evidence", '{"reason":"test rollback"}', check=False,
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(json.loads(failed.stderr)["error"], "forced event failure")
        shown = json.loads(self.run_cli("work", "show", "CS-115").stdout)
        self.assertEqual(shown["status"], "draft")

    def test_concurrent_event_writes_are_serialized(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "work", "create", "--id", "CS-116", "--title", "Concurrent events",
            "--mode", "scout", "--authority", "read-only",
        )

        def append(index: int) -> subprocess.CompletedProcess[str]:
            return self.run_cli(
                "event", "append", "CS-116", "--type", "worker-status",
                "--payload", json.dumps({"index": index}),
                "--idempotency-key", f"concurrent-{index}", check=False,
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(append, range(16)))

        self.assertTrue(all(result.returncode == 0 for result in results))
        events = json.loads(self.run_cli("event", "list", "CS-116").stdout)
        worker_events = [event for event in events if event["event_type"] == "worker-status"]
        self.assertEqual(len(worker_events), 16)


if __name__ == "__main__":
    unittest.main()

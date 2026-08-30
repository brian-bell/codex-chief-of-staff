from __future__ import annotations

import json
import multiprocessing
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from multiprocessing.connection import Connection
from pathlib import Path

from src.codex_chief_of_staff.state import Ledger, StateError


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "skills" / "chief-of-staff" / "scripts" / "chief-of-staff-state"
V1_SCHEMA = ROOT / "tests" / "fixtures" / "schema-v1.sql"

_CRASH_MIGRATIONS = (
    (1, ("SELECT 1",)),
    (
        2,
        (
            "CREATE TABLE crash_v2 (id INTEGER PRIMARY KEY)",
            "INSERT INTO crash_v2(id) VALUES (2)",
            "UPDATE projects SET name = 'During' WHERE id = 'p1'",
            "SELECT migration_crash_barrier()",
        ),
    ),
)


class _CrashBarrierLedger(Ledger):
    def __init__(self, path: Path, barrier: Connection | None) -> None:
        super().__init__(
            path,
            _migrations=_CRASH_MIGRATIONS,
            _schema_version=2,
        )
        self._barrier = barrier

    def connect(self) -> sqlite3.Connection:
        connection = super().connect()
        connection.create_function(
            "migration_crash_barrier",
            0,
            self._pause_inside_migration,
        )
        return connection

    def _pause_inside_migration(self) -> int:
        if self._barrier is not None:
            self._barrier.send("in-flight")
            self._barrier.recv()
        return 1


def _run_crashing_migration(db_path: str, barrier: Connection) -> None:
    try:
        _CrashBarrierLedger(Path(db_path), barrier).initialize()
    finally:
        barrier.close()


class _ConnectionSpy:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.closed = False

    def __enter__(self) -> _ConnectionSpy:
        return self

    def __exit__(self, *args: object) -> bool:
        return bool(self.connection.__exit__(*args))

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)

    def close(self) -> None:
        self.closed = True
        self.connection.close()


class _CloseSpyLedger(Ledger):
    last_connection: _ConnectionSpy | None = None

    def connect(self):
        connection = _ConnectionSpy(super().connect())
        self.last_connection = connection
        return connection


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db = Path(self.tempdir.name) / "state.db"

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), "--db", str(self.db), *args],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_fresh_database_has_complete_released_schema(self) -> None:
        result = Ledger(self.db).initialize()

        with sqlite3.connect(self.db) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            versions = connection.execute(
                "SELECT version FROM schema_versions ORDER BY version"
            ).fetchall()
        self.assertEqual(result, {"schema_version": 1})
        self.assertEqual(
            tables,
            {
                "events",
                "gates",
                "projects",
                "schema_versions",
                "sqlite_sequence",
                "task_links",
                "verdicts",
                "work_orders",
            },
        )
        self.assertEqual(versions, [(1,)])

    def test_pending_migrations_run_in_order_and_only_once(self) -> None:
        migrations = (
            (
                1,
                (
                    "CREATE TABLE migration_log (position INTEGER PRIMARY KEY)",
                    "INSERT INTO migration_log(position) VALUES (1)",
                ),
            ),
            (2, ("INSERT INTO migration_log(position) VALUES (2)",)),
            (3, ("INSERT INTO migration_log(position) VALUES (3)",)),
        )
        ledger = Ledger(self.db, _migrations=migrations, _schema_version=3)

        self.assertEqual(ledger.initialize(), {"schema_version": 3})
        with sqlite3.connect(self.db) as connection:
            first_versions = connection.execute(
                "SELECT version, applied_at FROM schema_versions ORDER BY version"
            ).fetchall()
            first_log = connection.execute(
                "SELECT position FROM migration_log ORDER BY position"
            ).fetchall()

        self.assertEqual(ledger.initialize(), {"schema_version": 3})
        with sqlite3.connect(self.db) as connection:
            second_versions = connection.execute(
                "SELECT version, applied_at FROM schema_versions ORDER BY version"
            ).fetchall()
            second_log = connection.execute(
                "SELECT position FROM migration_log ORDER BY position"
            ).fetchall()

        self.assertEqual(first_log, [(1,), (2,), (3,)])
        self.assertEqual(second_log, first_log)
        self.assertEqual(second_versions, first_versions)

    def test_failed_pending_batch_rolls_back_ddl_data_and_versions(self) -> None:
        Ledger(self.db).initialize()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO projects(id, name, created_at, updated_at) VALUES ('p1', 'Before', 1, 1)"
            )

        migrations = (
            (1, ("SELECT 1",)),
            (
                2,
                (
                    "CREATE TABLE migration_two (id INTEGER PRIMARY KEY)",
                    "UPDATE projects SET name = 'Changed' WHERE id = 'p1'",
                ),
            ),
            (
                3,
                (
                    "CREATE TABLE migration_three (id INTEGER PRIMARY KEY)",
                    "INSERT INTO missing_table(id) VALUES (1)",
                ),
            ),
        )

        with self.assertRaisesRegex(sqlite3.OperationalError, "missing_table"):
            Ledger(self.db, _migrations=migrations, _schema_version=3).initialize()

        with sqlite3.connect(self.db) as connection:
            project_name = connection.execute(
                "SELECT name FROM projects WHERE id = 'p1'"
            ).fetchone()[0]
            versions = connection.execute(
                "SELECT version FROM schema_versions ORDER BY version"
            ).fetchall()
            added_tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('migration_two', 'migration_three')"
            ).fetchall()
        self.assertEqual(project_name, "Before")
        self.assertEqual(versions, [(1,)])
        self.assertEqual(added_tables, [])

    def test_initialize_closes_its_connection_on_success_and_failure(self) -> None:
        successful = _CloseSpyLedger(self.db)
        self.assertEqual(successful.initialize(), {"schema_version": 1})
        self.assertIsNotNone(successful.last_connection)
        assert successful.last_connection is not None
        self.assertTrue(successful.last_connection.closed)

        failing = _CloseSpyLedger(
            self.db,
            _migrations=(
                (1, ("SELECT 1",)),
                (2, ("INSERT INTO missing_table(id) VALUES (2)",)),
            ),
            _schema_version=2,
        )
        with self.assertRaisesRegex(sqlite3.OperationalError, "missing_table"):
            failing.initialize()
        self.assertIsNotNone(failing.last_connection)
        assert failing.last_connection is not None
        self.assertTrue(failing.last_connection.closed)

    def test_migration_cannot_commit_outside_the_pending_batch(self) -> None:
        Ledger(self.db).initialize()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO projects(id, name, created_at, updated_at) "
                "VALUES ('p1', 'Before', 1, 1)"
            )
        escaping_migrations = (
            (1, ("SELECT 1",)),
            (
                2,
                (
                    "CREATE TABLE escaped_v2 (id INTEGER PRIMARY KEY)",
                    "INSERT INTO escaped_v2(id) VALUES (2)",
                    "/* mixed casing defeats prefix checks */ CoMmIt",
                ),
            ),
            (
                3,
                (
                    "CREATE TABLE rolled_v3 (id INTEGER PRIMARY KEY)",
                    "INSERT INTO missing_table(id) VALUES (3)",
                ),
            ),
        )

        with self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
            Ledger(
                self.db,
                _migrations=escaping_migrations,
                _schema_version=3,
            ).initialize()

        with sqlite3.connect(self.db, timeout=0.2) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_versions ORDER BY version"
                ).fetchall(),
                [(1,)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM projects WHERE id = 'p1'"
                ).fetchone(),
                ("Before",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name IN ('escaped_v2', 'rolled_v3')"
                ).fetchall(),
                [],
            )

        valid_retry = (
            (1, ("SELECT 1",)),
            (
                2,
                (
                    "CREATE TABLE escaped_v2 (id INTEGER PRIMARY KEY)",
                    "INSERT INTO escaped_v2(id) VALUES (2)",
                ),
            ),
            (3, ("CREATE TABLE rolled_v3 (id INTEGER PRIMARY KEY)",)),
        )
        self.assertEqual(
            Ledger(
                self.db,
                _migrations=valid_retry,
                _schema_version=3,
            ).initialize(),
            {"schema_version": 3},
        )
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT id FROM escaped_v2").fetchall(),
                [(2,)],
            )

    def test_migration_cannot_reshape_the_batch_with_a_savepoint(self) -> None:
        Ledger(self.db).initialize()
        migrations = (
            (1, ("SELECT 1",)),
            (
                2,
                (
                    "CREATE TABLE savepoint_v2 (id INTEGER PRIMARY KEY)",
                    "SaVePoInT nested_migration",
                    "INSERT INTO savepoint_v2(id) VALUES (2)",
                    "RELEASE nested_migration",
                ),
            ),
        )

        with self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
            Ledger(
                self.db,
                _migrations=migrations,
                _schema_version=2,
            ).initialize()

        with sqlite3.connect(self.db, timeout=0.2) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_versions ORDER BY version"
                ).fetchall(),
                [(1,)],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'savepoint_v2'"
                ).fetchone()
            )

    def test_released_v1_data_and_recovery_views_survive_reopen(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(V1_SCHEMA.read_text())
            connection.execute(
                "INSERT INTO schema_versions(version, applied_at) VALUES (1, 101)"
            )
            connection.execute(
                """
                INSERT INTO projects(
                    id, name, codex_project_id, repository, source_control,
                    default_branch, created_at, updated_at
                ) VALUES ('project-1', 'Ledger', 'codex-1', 'acme/ledger',
                          'github', 'main', 10, 11)
                """
            )
            connection.execute(
                """
                INSERT INTO work_orders(
                    id, title, mode, authority, status, project_id,
                    coordinator_task_id, branch, pull_request, head_sha,
                    created_at, updated_at
                ) VALUES ('CS-OPEN', 'Resume migration', 'build', 'local-write',
                          'running', 'project-1', 'chief-1', 'codex/migrate',
                          'https://example.test/pr/1', 'head-current', 20, 21)
                """
            )
            connection.execute(
                """
                INSERT INTO task_links(
                    work_order_id, task_id, role, host_id, environment, status,
                    brief_digest, created_at, updated_at
                ) VALUES ('CS-OPEN', 'worker-1', 'worker', 'local', 'worktree',
                          'running', 'sha256:brief', 30, 31)
                """
            )
            connection.executemany(
                """
                INSERT INTO gates(
                    id, work_order_id, question, status, answer, created_at, resolved_at
                ) VALUES (?, 'CS-OPEN', ?, ?, ?, ?, ?)
                """,
                [
                    ("gate-open", "Which option?", "open", None, 40, None),
                    ("gate-done", "Keep data?", "resolved", "Yes", 41, 42),
                ],
            )
            connection.executemany(
                """
                INSERT INTO verdicts(
                    id, work_order_id, reviewer_task_id, pull_request, head_sha,
                    verdict, risk, evidence, created_at
                ) VALUES (?, 'CS-OPEN', ?, 'https://example.test/pr/1', ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "verdict-stale",
                        "reviewer-old",
                        "head-old",
                        "fail",
                        "high",
                        '{"finding":"old"}',
                        50,
                    ),
                    (
                        "verdict-current",
                        "reviewer-new",
                        "head-current",
                        "pass-with-notes",
                        "medium",
                        '{"tests":["unit","race"]}',
                        51,
                    ),
                ],
            )
            connection.executemany(
                """
                INSERT INTO events(
                    sequence, work_order_id, source_task_id, event_type, payload,
                    idempotency_key, created_at
                ) VALUES (?, 'CS-OPEN', ?, ?, ?, ?, ?)
                """,
                [
                    (7, "worker-1", "worker-status", '{"status":"running"}', "evt-7", 60),
                    (9, "reviewer-new", "verdict-recorded", '{"verdict":"pass-with-notes"}', "evt-9", 61),
                ],
            )

        before_open = json.loads(self.run_cli("work", "list", "--open").stdout)
        before_verdict = json.loads(
            self.run_cli("verdict", "current", "CS-OPEN").stdout
        )
        before_events = json.loads(self.run_cli("event", "list", "CS-OPEN").stdout)

        self.assertEqual(json.loads(self.run_cli("init").stdout), {"schema_version": 1})
        after_open = json.loads(self.run_cli("work", "list", "--open").stdout)
        after_verdict = json.loads(
            self.run_cli("verdict", "current", "CS-OPEN").stdout
        )
        after_events = json.loads(self.run_cli("event", "list", "CS-OPEN").stdout)

        self.assertEqual(after_open, before_open)
        self.assertEqual(after_verdict, before_verdict)
        self.assertEqual(after_events, before_events)
        self.assertEqual([event["sequence"] for event in after_events], [7, 9])
        self.assertEqual(after_open[0]["project"]["id"], "project-1")
        self.assertEqual(after_open[0]["tasks"][0]["task_id"], "worker-1")
        self.assertEqual(after_open[0]["open_gates"][0]["id"], "gate-open")
        self.assertEqual(after_verdict["verdict"]["id"], "verdict-current")
        self.assertEqual(after_verdict["verdict"]["evidence"], {"tests": ["unit", "race"]})

        with sqlite3.connect(self.db) as connection:
            preserved = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "projects",
                    "work_orders",
                    "task_links",
                    "gates",
                    "verdicts",
                    "events",
                    "schema_versions",
                )
            }
            applied_at = connection.execute(
                "SELECT applied_at FROM schema_versions WHERE version = 1"
            ).fetchone()[0]
            resolved_gate = connection.execute(
                "SELECT status, answer, resolved_at FROM gates WHERE id = 'gate-done'"
            ).fetchone()
        self.assertEqual(
            preserved,
            {
                "projects": 1,
                "work_orders": 1,
                "task_links": 1,
                "gates": 2,
                "verdicts": 2,
                "events": 2,
                "schema_versions": 1,
            },
        )
        self.assertEqual(applied_at, 101)
        self.assertEqual(resolved_gate, ("resolved", "Yes", 42))

    def test_process_death_during_migration_rolls_back_and_retry_is_exactly_once(
        self,
    ) -> None:
        Ledger(self.db).initialize()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO projects(id, name, created_at, updated_at) "
                "VALUES ('p1', 'Before', 1, 1)"
            )

        context = multiprocessing.get_context("spawn")
        parent_barrier, child_barrier = context.Pipe()
        process = context.Process(
            target=_run_crashing_migration,
            args=(str(self.db), child_barrier),
        )
        process_started = False
        try:
            process.start()
            process_started = True
            child_barrier.close()
            self.assertTrue(
                parent_barrier.poll(10),
                "migration process did not reach the in-flight barrier",
            )
            self.assertEqual(parent_barrier.recv(), "in-flight")
            self.assertTrue(process.is_alive())
            process.kill()
            process.join(10)
            self.assertFalse(process.is_alive(), "killed migration process did not exit")
            self.assertIsNotNone(process.exitcode)
            self.assertNotEqual(process.exitcode, 0)
        finally:
            child_barrier.close()
            if process_started:
                if process.is_alive():
                    process.kill()
                process.join(10)
            parent_barrier.close()
            if process_started:
                process.close()

        with sqlite3.connect(self.db, timeout=0.2) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_versions ORDER BY version"
                ).fetchall(),
                [(1,)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM projects WHERE id = 'p1'"
                ).fetchone(),
                ("Before",),
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'crash_v2'"
                ).fetchone()
            )

        retry = _CrashBarrierLedger(self.db, None)
        self.assertEqual(retry.initialize(), {"schema_version": 2})
        self.assertEqual(retry.initialize(), {"schema_version": 2})
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT id FROM crash_v2").fetchall(),
                [(2,)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_versions ORDER BY version"
                ).fetchall(),
                [(1,), (2,)],
            )

    def test_concurrent_cli_initialization_creates_one_complete_schema(self) -> None:
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; sys.stdin.readline(); "
                        "from src.codex_chief_of_staff.cli import run; "
                        "raise SystemExit(run(['--db', sys.argv[1], 'init']))"
                    ),
                    str(self.db),
                ],
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(8)
        ]
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("start\n")
            process.stdin.close()
        results = []
        for process in processes:
            assert process.stdout is not None
            assert process.stderr is not None
            results.append((process.wait(timeout=10), process.stdout.read(), process.stderr.read()))
            process.stdout.close()
            process.stderr.close()

        self.assertEqual([result[0] for result in results], [0] * len(processes))
        self.assertEqual(
            [json.loads(result[1]) for result in results],
            [{"schema_version": 1}] * len(processes),
        )
        self.assertTrue(all(not result[2] for result in results))
        with sqlite3.connect(self.db) as connection:
            versions = connection.execute(
                "SELECT version FROM schema_versions ORDER BY version"
            ).fetchall()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertEqual(versions, [(1,)])
        self.assertTrue(
            {"projects", "work_orders", "events", "task_links", "verdicts", "gates"}
            <= tables
        )

    def test_concurrent_pending_migration_is_applied_once(self) -> None:
        runner = (
            "import json, sys; sys.stdin.readline(); "
            "from pathlib import Path; "
            "from src.codex_chief_of_staff.state import Ledger; "
            "m=((1,('CREATE TABLE marker (id INTEGER PRIMARY KEY)',)),"
            "(2,('INSERT INTO marker(id) VALUES (1)',))); "
            "print(json.dumps(Ledger(Path(sys.argv[1]), _migrations=m, "
            "_schema_version=2).initialize(), sort_keys=True))"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", runner, str(self.db)],
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(6)
        ]
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("start\n")
            process.stdin.close()
        results = []
        for process in processes:
            assert process.stdout is not None
            assert process.stderr is not None
            results.append((process.wait(timeout=10), process.stdout.read(), process.stderr.read()))
            process.stdout.close()
            process.stderr.close()

        self.assertEqual([result[0] for result in results], [0] * len(processes))
        self.assertEqual(
            [json.loads(result[1]) for result in results],
            [{"schema_version": 2}] * len(processes),
        )
        self.assertTrue(all(not result[2] for result in results))
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT id FROM marker").fetchall(), [(1,)]
            )
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_versions ORDER BY version"
                ).fetchall(),
                [(1,), (2,)],
            )

    def test_malformed_registries_fail_before_creating_a_database(self) -> None:
        cases = {
            "empty": ((), 1),
            "zero": (((0, ("SELECT 1",)),), 1),
            "duplicate": (((1, ("SELECT 1",)), (1, ("SELECT 1",))), 2),
            "gap": (((1, ("SELECT 1",)), (3, ("SELECT 1",))), 3),
            "out-of-order": (((2, ("SELECT 1",)), (1, ("SELECT 1",))), 2),
            "declared-mismatch": (((1, ("SELECT 1",)),), 2),
        }
        for name, (migrations, current_version) in cases.items():
            with self.subTest(name=name):
                db = Path(self.tempdir.name) / f"{name}.db"
                with self.assertRaises(StateError):
                    Ledger(
                        db,
                        _migrations=migrations,
                        _schema_version=current_version,
                    ).initialize()
                self.assertFalse(db.exists())

    def test_registry_versions_require_exact_integer_types(self) -> None:
        cases = {
            "boolean-migration": (((True, ("SELECT 1",)),), 1),
            "float-migration": (((1.0, ("SELECT 1",)),), 1),
            "boolean-current": (((1, ("SELECT 1",)),), True),
            "float-current": (((1, ("SELECT 1",)),), 1.0),
        }
        for name, (migrations, current_version) in cases.items():
            with self.subTest(name=name):
                db = Path(self.tempdir.name) / f"{name}.db"
                with self.assertRaisesRegex(StateError, "exact integers"):
                    Ledger(
                        db,
                        _migrations=migrations,
                        _schema_version=current_version,
                    ).initialize()
                self.assertFalse(db.exists())

    def test_non_prefix_database_history_is_rejected_without_mutation(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                [(1, 100), (3, 300)],
            )
        migrations = (
            (1, ("SELECT 1",)),
            (2, ("CREATE TABLE should_not_exist (id INTEGER)",)),
            (3, ("SELECT 3",)),
        )

        with self.assertRaisesRegex(StateError, "contiguous registry prefix"):
            Ledger(self.db, _migrations=migrations, _schema_version=3).initialize()

        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT version, applied_at FROM schema_versions ORDER BY version"
                ).fetchall(),
                [(1, 100), (3, 300)],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'should_not_exist'"
                ).fetchone()
            )

    def test_database_newer_than_code_is_rejected_without_mutation(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO schema_versions(version, applied_at) VALUES (2, 1234)"
            )

        with self.assertRaisesRegex(StateError, "newer than supported"):
            Ledger(self.db).initialize()

        with sqlite3.connect(self.db) as connection:
            versions = connection.execute(
                "SELECT version, applied_at FROM schema_versions ORDER BY version"
            ).fetchall()
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        self.assertEqual(versions, [(2, 1234)])
        self.assertEqual(tables, [("schema_versions",)])


if __name__ == "__main__":
    unittest.main()

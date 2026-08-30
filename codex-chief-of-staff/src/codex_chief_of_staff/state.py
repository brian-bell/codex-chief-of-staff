from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1

TRANSITIONS = {
    "draft": {"queued", "cancelled", "superseded"},
    "queued": {"dispatched", "cancelled", "superseded"},
    "dispatched": {"running", "needs-attention", "failed", "cancelled"},
    "running": {
        "needs-attention",
        "verifying",
        "review-required",
        "ready-to-publish",
        "blocked",
        "failed",
        "cancelled",
    },
    "needs-attention": {"running", "blocked", "cancelled"},
    "verifying": {"review-required", "ready-to-publish", "blocked", "failed"},
    "review-required": {"ready-to-publish", "blocked", "failed"},
    "ready-to-publish": {"published", "cancelled", "superseded"},
    "published": {"babysitting", "merge-ready", "done", "superseded"},
    "babysitting": {"needs-attention", "merge-ready", "blocked", "failed"},
    "merge-ready": {"awaiting-merge-authority", "landing", "done"},
    "awaiting-merge-authority": {"landing", "cancelled"},
    "landing": {"done", "blocked", "failed"},
}

AUTHORITY_CAPABILITIES = {
    "read-only": frozenset(),
    "local-write": frozenset({"local-write"}),
    "publish": frozenset({"local-write", "publish"}),
    "pr-maintenance": frozenset({"local-write", "publish", "babysit"}),
    "merge": frozenset({"merge"}),
    "triage-write": frozenset({"triage-write"}),
}

MODE_ORDER = {
    "scout": 0,
    "build": 1,
    "review": 2,
    "publish": 3,
    "babysit": 4,
    "land": 5,
    "triage": 0,
}

STATUS_AUTHORITY = {
    "published": "publish",
    "babysitting": "babysit",
    "landing": "merge",
}

GATE_BLOCKED_STATUSES = {
    "ready-to-publish",
    "published",
    "merge-ready",
    "awaiting-merge-authority",
    "landing",
    "done",
}

TERMINAL_STATUSES = {"done", "blocked", "cancelled", "failed", "superseded"}


class StateError(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> dict[str, int]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_versions (
                    version INTEGER PRIMARY KEY,
                    applied_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    codex_project_id TEXT,
                    repository TEXT,
                    source_control TEXT,
                    default_branch TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS work_orders (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    status TEXT NOT NULL,
                    project_id TEXT REFERENCES projects(id),
                    coordinator_task_id TEXT,
                    branch TEXT,
                    pull_request TEXT,
                    head_sha TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_order_id TEXT NOT NULL REFERENCES work_orders(id),
                    source_task_id TEXT,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_links (
                    work_order_id TEXT NOT NULL REFERENCES work_orders(id),
                    task_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    host_id TEXT,
                    environment TEXT NOT NULL,
                    status TEXT NOT NULL,
                    brief_digest TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (work_order_id, task_id),
                    UNIQUE (work_order_id, role)
                );

                CREATE TABLE IF NOT EXISTS verdicts (
                    id TEXT PRIMARY KEY,
                    work_order_id TEXT NOT NULL REFERENCES work_orders(id),
                    reviewer_task_id TEXT,
                    pull_request TEXT,
                    head_sha TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gates (
                    id TEXT PRIMARY KEY,
                    work_order_id TEXT NOT NULL REFERENCES work_orders(id),
                    question TEXT NOT NULL,
                    status TEXT NOT NULL,
                    answer TEXT,
                    created_at INTEGER NOT NULL,
                    resolved_at INTEGER
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_versions(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _now()),
            )
        return {"schema_version": SCHEMA_VERSION}

    def create_work_order(
        self,
        *,
        work_id: str,
        title: str,
        mode: str,
        authority: str,
        project_id: str | None = None,
        coordinator_task_id: str | None = None,
    ) -> dict[str, Any]:
        if authority not in AUTHORITY_CAPABILITIES:
            raise StateError(f"unknown authority: {authority}")
        if mode not in MODE_ORDER:
            raise StateError(f"unknown mode: {mode}")
        now = _now()
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO work_orders(
                        id, title, mode, authority, status, project_id,
                        coordinator_task_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?)
                    """,
                    (
                        work_id,
                        title,
                        mode,
                        authority,
                        project_id,
                        coordinator_task_id,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError(f"work order already exists: {work_id}") from exc
        return self.show_work_order(work_id)

    def show_work_order(self, work_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_orders WHERE id = ?", (work_id,)
            ).fetchone()
        if row is None:
            raise StateError(f"unknown work order: {work_id}")
        return dict(row)

    def list_work_orders(self, *, open_only: bool) -> list[dict[str, Any]]:
        query = """
            SELECT w.*, p.id AS p_id, p.name AS p_name,
                   p.codex_project_id AS p_codex_project_id,
                   p.repository AS p_repository,
                   p.source_control AS p_source_control,
                   p.default_branch AS p_default_branch
            FROM work_orders w LEFT JOIN projects p ON p.id = w.project_id
        """
        parameters: tuple[Any, ...] = ()
        if open_only:
            placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
            query += f" WHERE w.status NOT IN ({placeholders})"
            parameters = tuple(sorted(TERMINAL_STATUSES))
        query += " ORDER BY w.updated_at DESC, w.id"
        results: list[dict[str, Any]] = []
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            for row in rows:
                value = {
                    key: row[key] for key in row.keys() if not key.startswith("p_")
                }
                value["project"] = (
                    {
                        "id": row["p_id"],
                        "name": row["p_name"],
                        "codex_project_id": row["p_codex_project_id"],
                        "repository": row["p_repository"],
                        "source_control": row["p_source_control"],
                        "default_branch": row["p_default_branch"],
                    }
                    if row["p_id"] is not None
                    else None
                )
                value["tasks"] = [
                    dict(task)
                    for task in connection.execute(
                        """
                        SELECT * FROM task_links
                        WHERE work_order_id = ? ORDER BY role, task_id
                        """,
                        (row["id"],),
                    ).fetchall()
                ]
                value["open_gates"] = [
                    dict(gate)
                    for gate in connection.execute(
                        """
                        SELECT * FROM gates
                        WHERE work_order_id = ? AND status = 'open'
                        ORDER BY created_at, id
                        """,
                        (row["id"],),
                    ).fetchall()
                ]
                current_verdict = connection.execute(
                    """
                    SELECT * FROM verdicts
                    WHERE work_order_id = ? AND head_sha = ?
                    ORDER BY created_at DESC, rowid DESC LIMIT 1
                    """,
                    (row["id"], row["head_sha"]),
                ).fetchone()
                value["current_verdict"] = (
                    _verdict_dict(current_verdict)
                    if current_verdict is not None
                    else None
                )
                results.append(value)
        return results

    def put_project(
        self,
        *,
        project_id: str,
        name: str,
        codex_project_id: str | None,
        repository: str | None,
        source_control: str | None,
        default_branch: str | None,
    ) -> dict[str, Any]:
        now = _now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO projects(
                    id, name, codex_project_id, repository, source_control,
                    default_branch, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    codex_project_id = excluded.codex_project_id,
                    repository = excluded.repository,
                    source_control = excluded.source_control,
                    default_branch = excluded.default_branch,
                    updated_at = excluded.updated_at
                """,
                (
                    project_id,
                    name,
                    codex_project_id,
                    repository,
                    source_control,
                    default_branch,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return dict(row)

    def transition_work_order(
        self, *, work_id: str, target: str, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        if not evidence:
            raise StateError("transition evidence must be a non-empty JSON object")
        now = _now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status, authority FROM work_orders WHERE id = ?", (work_id,)
            ).fetchone()
            if row is None:
                raise StateError(f"unknown work order: {work_id}")
            current = row["status"]
            if target not in TRANSITIONS.get(current, set()):
                raise StateError(f"invalid transition: {current} -> {target}")
            if target == "dispatched" and connection.execute(
                "SELECT 1 FROM task_links WHERE work_order_id = ? LIMIT 1", (work_id,)
            ).fetchone() is None:
                raise StateError("dispatched transition requires a recorded task link")
            if target in GATE_BLOCKED_STATUSES:
                open_gate_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM gates
                    WHERE work_order_id = ? AND status = 'open'
                    """,
                    (work_id,),
                ).fetchone()[0]
                if open_gate_count:
                    raise StateError(
                        f"transition to {target} blocked by {open_gate_count} open gates"
                    )
            required_authority = STATUS_AUTHORITY.get(target)
            if required_authority and required_authority not in AUTHORITY_CAPABILITIES[
                row["authority"]
            ]:
                raise StateError(f"transition to {target} requires {required_authority} authority")
            if target == "landing":
                work_head = connection.execute(
                    "SELECT head_sha FROM work_orders WHERE id = ?", (work_id,)
                ).fetchone()["head_sha"]
                latest_verdict = connection.execute(
                    """
                    SELECT verdict FROM verdicts
                    WHERE work_order_id = ? AND head_sha = ?
                    ORDER BY created_at DESC, rowid DESC LIMIT 1
                    """,
                    (work_id, work_head),
                ).fetchone()
                if latest_verdict is None:
                    raise StateError("landing requires a current passing verdict")
                if latest_verdict["verdict"] not in {"pass", "pass-with-notes"}:
                    raise StateError(
                        "latest verdict for current head is "
                        f"{latest_verdict['verdict']}, not passing"
                    )
            artifact_updates: dict[str, str] = {}
            if target == "published":
                for field in ("pull_request", "head_sha"):
                    value = evidence.get(field)
                    if not isinstance(value, str) or not value.strip():
                        raise StateError(f"published transition requires evidence.{field}")
                    artifact_updates[field] = value
            connection.execute(
                """
                UPDATE work_orders
                SET status = ?, pull_request = COALESCE(?, pull_request),
                    head_sha = COALESCE(?, head_sha), updated_at = ?
                WHERE id = ?
                """,
                (
                    target,
                    artifact_updates.get("pull_request"),
                    artifact_updates.get("head_sha"),
                    now,
                    work_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO events(work_order_id, event_type, payload, created_at)
                VALUES (?, 'state-transition', ?, ?)
                """,
                (work_id, json_line({"from": current, "to": target, "evidence": evidence}), now),
            )
        return self.show_work_order(work_id)

    def promote_work_order(
        self,
        *,
        work_id: str,
        mode: str,
        authority: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if not evidence:
            raise StateError("promotion evidence must be a non-empty JSON object")
        if mode not in MODE_ORDER:
            raise StateError(f"unknown mode: {mode}")
        if authority not in AUTHORITY_CAPABILITIES:
            raise StateError(f"unknown authority: {authority}")
        now = _now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT mode, authority, status FROM work_orders WHERE id = ?", (work_id,)
            ).fetchone()
            if row is None:
                raise StateError(f"unknown work order: {work_id}")
            if row["status"] in TERMINAL_STATUSES:
                raise StateError(f"cannot promote terminal work order: {row['status']}")
            if MODE_ORDER[mode] < MODE_ORDER[row["mode"]]:
                raise StateError("promotion cannot reduce mode")
            connection.execute(
                "UPDATE work_orders SET mode = ?, authority = ?, updated_at = ? WHERE id = ?",
                (mode, authority, now, work_id),
            )
            connection.execute(
                """
                INSERT INTO events(work_order_id, event_type, payload, created_at)
                VALUES (?, 'authority-promoted', ?, ?)
                """,
                (
                    work_id,
                    json_line(
                        {
                            "from": {"mode": row["mode"], "authority": row["authority"]},
                            "to": {"mode": mode, "authority": authority},
                            "evidence": evidence,
                        }
                    ),
                    now,
                ),
            )
        return self.show_work_order(work_id)

    def record_dispatch(
        self,
        *,
        work_id: str,
        task_id: str,
        role: str,
        host_id: str | None,
        environment: str,
        brief_digest: str,
    ) -> dict[str, Any]:
        now = _now()
        callback_statuses = [
            "needs-attention",
            "blocked",
            "failed",
            "cancelled",
            "done",
        ]
        requested = {
            "work_order_id": work_id,
            "task_id": task_id,
            "role": role,
            "host_id": host_id,
            "environment": environment,
            "status": "dispatched",
            "brief_digest": brief_digest,
        }
        with self.connect() as connection:
            work = connection.execute(
                "SELECT coordinator_task_id FROM work_orders WHERE id = ?", (work_id,)
            ).fetchone()
            if work is None:
                raise StateError(f"unknown work order: {work_id}")
            if not work["coordinator_task_id"]:
                raise StateError(
                    "dispatch requires a coordinator task ID for worker callbacks"
                )
            callback = {
                "task_id": work["coordinator_task_id"],
                "work_id": work_id,
                "statuses": callback_statuses,
            }
            existing = connection.execute(
                "SELECT * FROM task_links WHERE work_order_id = ? AND role = ?",
                (work_id, role),
            ).fetchone()
            if existing is not None:
                existing_values = {key: existing[key] for key in requested}
                if existing_values == requested:
                    return {
                        **existing_values,
                        "callback": callback,
                        "idempotent": True,
                    }
                raise StateError(
                    f"role already linked for {work_id}: {role} -> {existing['task_id']}"
                )
            connection.execute(
                """
                INSERT INTO task_links(
                    work_order_id, task_id, role, host_id, environment, status,
                    brief_digest, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'dispatched', ?, ?, ?)
                """,
                (work_id, task_id, role, host_id, environment, brief_digest, now, now),
            )
            connection.execute(
                """
                INSERT INTO events(work_order_id, source_task_id, event_type, payload, created_at)
                VALUES (?, ?, 'task-dispatched', ?, ?)
                """,
                (
                    work_id,
                    task_id,
                    json_line(
                        {
                            "role": role,
                            "host_id": host_id,
                            "environment": environment,
                            "brief_digest": brief_digest,
                            "callback": callback,
                        }
                    ),
                    now,
                ),
            )
        return {**requested, "callback": callback, "idempotent": False}

    def set_head(
        self, *, work_id: str, head_sha: str, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        if not head_sha.strip():
            raise StateError("head SHA must not be empty")
        if not evidence:
            raise StateError("head update evidence must be a non-empty JSON object")
        now = _now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT head_sha FROM work_orders WHERE id = ?", (work_id,)
            ).fetchone()
            if row is None:
                raise StateError(f"unknown work order: {work_id}")
            connection.execute(
                "UPDATE work_orders SET head_sha = ?, updated_at = ? WHERE id = ?",
                (head_sha, now, work_id),
            )
            connection.execute(
                """
                INSERT INTO events(work_order_id, event_type, payload, created_at)
                VALUES (?, 'head-updated', ?, ?)
                """,
                (
                    work_id,
                    json_line(
                        {"from": row["head_sha"], "to": head_sha, "evidence": evidence}
                    ),
                    now,
                ),
            )
        return self.show_work_order(work_id)

    def record_verdict(
        self,
        *,
        verdict_id: str,
        work_id: str,
        head_sha: str,
        verdict: str,
        risk: str,
        reviewer_task_id: str | None,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if verdict not in {"pass", "pass-with-notes", "fail", "blocked"}:
            raise StateError(f"unknown verdict: {verdict}")
        if risk not in {"low", "medium", "high", "critical"}:
            raise StateError(f"unknown risk: {risk}")
        if not evidence:
            raise StateError("verdict evidence must be a non-empty JSON object")
        now = _now()
        with self.connect() as connection:
            work = connection.execute(
                "SELECT pull_request, head_sha FROM work_orders WHERE id = ?", (work_id,)
            ).fetchone()
            if work is None:
                raise StateError(f"unknown work order: {work_id}")
            if work["head_sha"] != head_sha:
                raise StateError(
                    f"verdict head does not match current work head: {head_sha} != {work['head_sha']}"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO verdicts(
                        id, work_order_id, reviewer_task_id, pull_request, head_sha,
                        verdict, risk, evidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        verdict_id,
                        work_id,
                        reviewer_task_id,
                        work["pull_request"],
                        head_sha,
                        verdict,
                        risk,
                        json_line(evidence),
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StateError(f"verdict already exists: {verdict_id}") from exc
            connection.execute(
                """
                INSERT INTO events(work_order_id, source_task_id, event_type, payload, created_at)
                VALUES (?, ?, 'verdict-recorded', ?, ?)
                """,
                (
                    work_id,
                    reviewer_task_id,
                    json_line(
                        {
                            "verdict_id": verdict_id,
                            "head_sha": head_sha,
                            "verdict": verdict,
                            "risk": risk,
                        }
                    ),
                    now,
                ),
            )
        return self._show_verdict(verdict_id)

    def current_verdict(self, work_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            work = connection.execute(
                "SELECT head_sha FROM work_orders WHERE id = ?", (work_id,)
            ).fetchone()
            if work is None:
                raise StateError(f"unknown work order: {work_id}")
            current = connection.execute(
                """
                SELECT * FROM verdicts
                WHERE work_order_id = ? AND head_sha = ?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (work_id, work["head_sha"]),
            ).fetchone()
            stale = connection.execute(
                """
                SELECT * FROM verdicts
                WHERE work_order_id = ? AND head_sha IS NOT ?
                ORDER BY created_at DESC, id DESC
                """,
                (work_id, work["head_sha"]),
            ).fetchall()
        return {
            "applicable": current is not None,
            "head_sha": work["head_sha"],
            "verdict": _verdict_dict(current) if current is not None else None,
            "stale_verdicts": [_verdict_dict(row) for row in stale],
        }

    def _show_verdict(self, verdict_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM verdicts WHERE id = ?", (verdict_id,)
            ).fetchone()
        if row is None:
            raise StateError(f"unknown verdict: {verdict_id}")
        return _verdict_dict(row)

    def open_gate(self, *, gate_id: str, work_id: str, question: str) -> dict[str, Any]:
        now = _now()
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO gates(id, work_order_id, question, status, created_at)
                    VALUES (?, ?, ?, 'open', ?)
                    """,
                    (gate_id, work_id, question, now),
                )
                connection.execute(
                    """
                    INSERT INTO events(work_order_id, event_type, payload, created_at)
                    VALUES (?, 'gate-opened', ?, ?)
                    """,
                    (work_id, json_line({"gate_id": gate_id, "question": question}), now),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError(f"cannot open gate {gate_id}: {exc}") from exc
        return self._show_gate(gate_id)

    def resolve_gate(self, *, gate_id: str, answer: str) -> dict[str, Any]:
        if not answer.strip():
            raise StateError("gate answer must not be empty")
        now = _now()
        with self.connect() as connection:
            gate = connection.execute(
                "SELECT * FROM gates WHERE id = ?", (gate_id,)
            ).fetchone()
            if gate is None:
                raise StateError(f"unknown gate: {gate_id}")
            if gate["status"] != "open":
                raise StateError(f"gate is already {gate['status']}: {gate_id}")
            connection.execute(
                """
                UPDATE gates SET status = 'resolved', answer = ?, resolved_at = ?
                WHERE id = ?
                """,
                (answer, now, gate_id),
            )
            connection.execute(
                """
                INSERT INTO events(work_order_id, event_type, payload, created_at)
                VALUES (?, 'gate-resolved', ?, ?)
                """,
                (
                    gate["work_order_id"],
                    json_line({"gate_id": gate_id, "answer": answer}),
                    now,
                ),
            )
        return self._show_gate(gate_id)

    def _show_gate(self, gate_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
        if row is None:
            raise StateError(f"unknown gate: {gate_id}")
        return dict(row)

    def append_event(
        self,
        *,
        work_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        source_task_id: str | None,
    ) -> dict[str, Any]:
        if not payload:
            raise StateError("event payload must be a non-empty JSON object")
        encoded_payload = json_line(payload)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                fields_match = (
                    existing["work_order_id"] == work_id
                    and existing["event_type"] == event_type
                    and existing["payload"] == encoded_payload
                    and existing["source_task_id"] == source_task_id
                )
                if not fields_match:
                    raise StateError(f"idempotency key conflict: {idempotency_key}")
                return {**_event_dict(existing), "idempotent": True}
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO events(
                        work_order_id, source_task_id, event_type, payload,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        work_id,
                        source_task_id,
                        event_type,
                        encoded_payload,
                        idempotency_key,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StateError(f"cannot append event: {exc}") from exc
            row = connection.execute(
                "SELECT * FROM events WHERE sequence = ?", (cursor.lastrowid,)
            ).fetchone()
        return {**_event_dict(row), "idempotent": False}

    def list_events(self, work_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE work_order_id = ? ORDER BY sequence",
                (work_id,),
            ).fetchall()
        return [_event_dict(row) for row in rows]


def _now() -> int:
    return int(time.time())


def json_line(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _verdict_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["evidence"] = json.loads(value["evidence"])
    return value


def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["payload"] = json.loads(value["payload"])
    return value

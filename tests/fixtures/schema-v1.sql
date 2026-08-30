-- Released schema v1 from commit ca9fa72b671fa2c2d51843b4832a5cb1029ea024.
-- The source state.py SHA-256 is 65727f1a9525586dbf76a4c8f6db8796f987f9cc962d156c1f2113941f2fbf0c.
-- Keep this fixture independent of the production migration registry.
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

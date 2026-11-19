CREATE TABLE local_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL UNIQUE,
    agent_id TEXT UNIQUE,
    scope TEXT NOT NULL CHECK(scope IN ('platform','personal')),
    kind TEXT NOT NULL CHECK(kind IN ('agent','skill')),
    revision INTEGER NOT NULL,
    cloud_revision INTEGER NOT NULL DEFAULT 0,
    desired_revision INTEGER,
    installed_revision INTEGER,
    spec TEXT NOT NULL,
    installed_snapshot TEXT,
    source_id TEXT,
    source_revision INTEGER,
    sync_state TEXT NOT NULL,
    install_state TEXT NOT NULL,
    error_code TEXT
);
CREATE TABLE local_resource_versions (
    resource_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    PRIMARY KEY(resource_id,revision)
);
CREATE TABLE local_resource_outbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL UNIQUE,
    resource_id TEXT NOT NULL REFERENCES local_resources(resource_id),
    mutation TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE local_resource_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
ALTER TABLE local_agents ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE local_agents ADD COLUMN resource_snapshot TEXT;
ALTER TABLE local_agents ADD COLUMN skills_path TEXT;

ALTER TABLE local_agents ADD COLUMN resource_id TEXT REFERENCES local_resources(resource_id);

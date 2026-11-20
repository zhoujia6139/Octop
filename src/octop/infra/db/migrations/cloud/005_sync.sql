CREATE TABLE cloud_sync_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES cloud_users(user_id),
    resource_id TEXT NOT NULL REFERENCES cloud_resources(resource_id)
);
CREATE TABLE cloud_sync_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES cloud_users(user_id),
    agent_id TEXT NOT NULL REFERENCES cloud_sync_agents(agent_id),
    kind TEXT NOT NULL CHECK(kind IN ('thread','message','file','memory')),
    revision INTEGER NOT NULL
);
CREATE TABLE cloud_sync_versions (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES cloud_sync_items(item_id),
    revision INTEGER NOT NULL,
    data TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    digest TEXT NOT NULL,
    UNIQUE(item_id,revision)
);
CREATE TABLE cloud_sync_operations (
    user_id INTEGER NOT NULL REFERENCES cloud_users(user_id),
    device_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY(user_id,device_id,generation,operation_id)
);
CREATE TABLE cloud_sync_blobs (
    user_id INTEGER NOT NULL REFERENCES cloud_users(user_id),
    digest TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id,digest)
);
CREATE TABLE cloud_sync_chunks (
    user_id INTEGER NOT NULL,
    digest TEXT NOT NULL,
    offset_bytes BIGINT NOT NULL,
    content BLOB NOT NULL,
    PRIMARY KEY(user_id,digest,offset_bytes),
    FOREIGN KEY(user_id,digest) REFERENCES cloud_sync_blobs(user_id,digest)
);
CREATE INDEX cloud_sync_owner ON cloud_sync_items(user_id,id);

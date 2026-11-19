CREATE TABLE local_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    model_id TEXT NOT NULL,
    system_prompt TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE local_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL REFERENCES local_agents(agent_id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL
);
CREATE TABLE local_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL REFERENCES local_threads(thread_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('running','awaiting_approval','completed','cancelled','interrupted','failed')),
    started_at INTEGER NOT NULL,
    finished_at INTEGER
);

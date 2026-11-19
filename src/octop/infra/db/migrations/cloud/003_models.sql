CREATE TABLE cloud_model_providers (
 provider_id TEXT PRIMARY KEY, base_url TEXT NOT NULL, credential_enc BLOB NOT NULL, free_statuses TEXT NOT NULL
);
CREATE TABLE cloud_model_rates (
 revision TEXT PRIMARY KEY, definition TEXT NOT NULL
);
CREATE TABLE cloud_models (
 id INTEGER PRIMARY KEY AUTOINCREMENT, model_id TEXT NOT NULL UNIQUE, definition TEXT NOT NULL
);
CREATE TABLE cloud_model_grants (
 user_id INTEGER NOT NULL REFERENCES cloud_users(user_id), model_id TEXT NOT NULL REFERENCES cloud_models(model_id),
 allowed INTEGER NOT NULL CHECK(allowed IN (0,1)), PRIMARY KEY(user_id,model_id)
);
CREATE TABLE cloud_model_budgets (
 user_id INTEGER NOT NULL REFERENCES cloud_users(user_id), currency TEXT NOT NULL,
 limit_nanos BIGINT NOT NULL, spent_nanos BIGINT NOT NULL DEFAULT 0, reserved_nanos BIGINT NOT NULL DEFAULT 0 CHECK(reserved_nanos>=0),
 PRIMARY KEY(user_id,currency)
);
CREATE TABLE cloud_model_agents (
 agent_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES cloud_users(user_id)
);
CREATE TABLE cloud_model_threads (
 thread_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES cloud_model_agents(agent_id)
);
CREATE TABLE cloud_model_turns (
 turn_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES cloud_model_threads(thread_id)
);
CREATE TABLE cloud_model_requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE,
 user_id INTEGER NOT NULL REFERENCES cloud_users(user_id), device_id TEXT NOT NULL, generation INTEGER NOT NULL,
 agent_id TEXT NOT NULL REFERENCES cloud_model_agents(agent_id), thread_id TEXT NOT NULL REFERENCES cloud_model_threads(thread_id),
 turn_id TEXT NOT NULL REFERENCES cloud_model_turns(turn_id), model_id TEXT NOT NULL REFERENCES cloud_models(model_id),
 purpose TEXT NOT NULL, request_hash TEXT NOT NULL, state TEXT NOT NULL,
 currency TEXT NOT NULL, rate_revision TEXT NOT NULL REFERENCES cloud_model_rates(revision), definition TEXT NOT NULL,
 created_at BIGINT NOT NULL, lease_until BIGINT NOT NULL
);
CREATE TABLE cloud_model_attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL UNIQUE,
 request_id TEXT NOT NULL REFERENCES cloud_model_requests(request_id), provider_id TEXT NOT NULL,
 state TEXT NOT NULL, reserved_nanos BIGINT NOT NULL, cost_nanos BIGINT, usage TEXT, reason TEXT,
 evidence TEXT, resolved_by INTEGER REFERENCES cloud_users(user_id)
);
CREATE TABLE cloud_model_settlements (
 id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL UNIQUE REFERENCES cloud_model_attempts(attempt_id),
 user_id INTEGER NOT NULL REFERENCES cloud_users(user_id), model_id TEXT NOT NULL REFERENCES cloud_models(model_id),
 currency TEXT NOT NULL, rate_revision TEXT NOT NULL REFERENCES cloud_model_rates(revision), cost_nanos BIGINT NOT NULL,
 uncached_input BIGINT NOT NULL, cache_read BIGINT NOT NULL, cache_write BIGINT NOT NULL, output BIGINT NOT NULL, created_at BIGINT NOT NULL
);
CREATE INDEX cloud_model_requests_owner ON cloud_model_requests(user_id,created_at);
CREATE INDEX cloud_model_requests_lease ON cloud_model_requests(state,lease_until);
CREATE INDEX cloud_model_attempts_request ON cloud_model_attempts(request_id);
CREATE INDEX cloud_model_settlements_owner ON cloud_model_settlements(user_id,created_at);

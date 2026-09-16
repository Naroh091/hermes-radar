PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 kind TEXT NOT NULL, account_ref TEXT NOT NULL, resource_scope_json TEXT NOT NULL,
 enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)), permission_revision INTEGER NOT NULL CHECK(permission_revision >= 1),
 cursor_json TEXT NOT NULL, last_success_at TEXT, retention_days INTEGER NOT NULL CHECK(retention_days >= 1),
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS grants (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 permission TEXT NOT NULL CHECK(permission IN ('observe', 'read', 'notify')),
 source_id TEXT, operation_ids_json TEXT NOT NULL, resource_scope_json TEXT NOT NULL,
 destination_id TEXT, limits_json TEXT NOT NULL, expires_at TEXT, revoked_at TEXT,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, source_id) REFERENCES sources(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, destination_id) REFERENCES destinations(profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS observations (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 source_id TEXT NOT NULL, source_event_key TEXT NOT NULL, occurred_at TEXT NOT NULL,
 received_at TEXT NOT NULL, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
 facts_json TEXT NOT NULL, expires_at TEXT, origin TEXT NOT NULL CHECK(origin IN ('user', 'tool', 'external', 'plugin')),
 correlation_id TEXT,
 PRIMARY KEY (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, source_id, source_event_key),
 FOREIGN KEY (profile_key, owner_id, source_id) REFERENCES sources(profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS evidence (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 observation_id TEXT NOT NULL, source_ref TEXT NOT NULL, excerpt_redacted TEXT NOT NULL,
 epistemic_status TEXT NOT NULL CHECK(epistemic_status IN ('documented', 'user_stated', 'inferred', 'disputed', 'retracted')),
 supersedes_id TEXT, expires_at TEXT,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, observation_id) REFERENCES observations(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, supersedes_id) REFERENCES evidence(profile_key, owner_id, id)
);

CREATE INDEX IF NOT EXISTS idx_observation_fingerprint
 ON observations(profile_key, owner_id, fingerprint, occurred_at);

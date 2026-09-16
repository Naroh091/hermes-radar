PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sources (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
 kind TEXT NOT NULL, account_ref TEXT NOT NULL, resource_scope_json TEXT NOT NULL,
 enabled INTEGER NOT NULL, permission_revision INTEGER NOT NULL, cursor_json TEXT NOT NULL,
 last_success_at TEXT, retention_days INTEGER NOT NULL,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id)
);
CREATE TABLE IF NOT EXISTS observations (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
 source_id TEXT NOT NULL, source_event_key TEXT NOT NULL, occurred_at TEXT NOT NULL,
 received_at TEXT NOT NULL, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
 facts_json TEXT NOT NULL, expires_at TEXT, origin TEXT NOT NULL, correlation_id TEXT,
 PRIMARY KEY (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, source_id, source_event_key),
 FOREIGN KEY (profile_key, owner_id, source_id) REFERENCES sources(profile_key, owner_id, id)
);
CREATE TABLE IF NOT EXISTS goals (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
 title TEXT NOT NULL, status TEXT NOT NULL, scope_json TEXT NOT NULL,
 user_intent_ref TEXT, parent_goal_id TEXT,
 PRIMARY KEY (profile_key, owner_id, id)
);
CREATE TABLE IF NOT EXISTS followups (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
 goal_id TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
 subject_key TEXT NOT NULL, evidence_revision INTEGER NOT NULL, next_check_at TEXT,
 deadline_at TEXT, expires_at TEXT NOT NULL, timezone TEXT NOT NULL,
 confidence_band TEXT NOT NULL, assumptions_json TEXT NOT NULL, stop_rule_json TEXT NOT NULL,
 last_notified_at TEXT, reminder_count INTEGER NOT NULL,
 PRIMARY KEY (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, subject_key),
 FOREIGN KEY (profile_key, owner_id, goal_id) REFERENCES goals(profile_key, owner_id, id)
);

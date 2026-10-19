CREATE TABLE IF NOT EXISTS patterns (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 kind TEXT NOT NULL CHECK(kind IN ('periodic', 'sequence')),
 fingerprint TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
 evidence_json TEXT NOT NULL DEFAULT '{}', support INTEGER NOT NULL DEFAULT 0 CHECK(support >= 0),
 confidence REAL NOT NULL DEFAULT 0,
 status TEXT NOT NULL CHECK(status IN ('candidate', 'proposed', 'automated', 'dismissed', 'expired')),
 task_id TEXT, automation_ref TEXT, last_seen_at TEXT,
 PRIMARY KEY (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_patterns_status ON patterns(profile_key, owner_id, status);

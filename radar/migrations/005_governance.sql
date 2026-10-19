CREATE TABLE IF NOT EXISTS feedback (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 target_type TEXT NOT NULL, target_id TEXT NOT NULL, action TEXT NOT NULL,
 reason TEXT, snooze_until TEXT,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS audit (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 event_type TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT,
 correlation_id TEXT, actor_kind TEXT NOT NULL, decision_code TEXT NOT NULL,
 metadata_redacted_json TEXT NOT NULL,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS budgets (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 bucket_type TEXT NOT NULL, bucket_start TEXT NOT NULL, resource_key TEXT NOT NULL,
 reserved_units INTEGER NOT NULL DEFAULT 0 CHECK(reserved_units >= 0),
 consumed_units INTEGER NOT NULL DEFAULT 0 CHECK(consumed_units >= 0), unit TEXT NOT NULL,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, bucket_type, bucket_start, resource_key, unit)
);

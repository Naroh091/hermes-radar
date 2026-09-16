CREATE TABLE IF NOT EXISTS audit (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
 event_type TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT,
 correlation_id TEXT, actor_kind TEXT NOT NULL, decision_code TEXT NOT NULL,
 metadata_redacted_json TEXT NOT NULL, PRIMARY KEY (profile_key, owner_id, id)
);
CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(profile_key, owner_id, status, next_check_at);
CREATE INDEX IF NOT EXISTS idx_observation_fingerprint ON observations(profile_key, owner_id, fingerprint, occurred_at);

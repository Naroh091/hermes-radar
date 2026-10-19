CREATE TABLE IF NOT EXISTS goals (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'completed', 'cancelled')),
 scope_json TEXT NOT NULL, user_intent_ref TEXT, parent_goal_id TEXT,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, parent_goal_id) REFERENCES goals(profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS followups (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 goal_id TEXT NOT NULL, reason TEXT NOT NULL CHECK(reason IN ('condition_wait', 'missing_input', 'next_step', 'agent_commitment', 'routine', 'external_signal')),
 status TEXT NOT NULL CHECK(status IN ('candidate', 'ready', 'waiting_input', 'waiting_event', 'suppressed', 'checking', 'awaiting_consent', 'completed', 'failed', 'paused', 'cancelled', 'expired')),
 subject_key TEXT NOT NULL, evidence_revision INTEGER NOT NULL CHECK(evidence_revision >= 1), next_check_at TEXT,
 deadline_at TEXT, expires_at TEXT NOT NULL, timezone TEXT NOT NULL,
 confidence_band TEXT NOT NULL CHECK(confidence_band IN ('low', 'medium', 'high')),
 assumptions_json TEXT NOT NULL, stop_rule_json TEXT NOT NULL,
 last_notified_at TEXT, reminder_count INTEGER NOT NULL DEFAULT 0 CHECK(reminder_count >= 0),
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, goal_id) REFERENCES goals(profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS followup_evidence (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 followup_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('supports', 'contradicts', 'blocks')),
 PRIMARY KEY (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, followup_id, evidence_id, role),
 FOREIGN KEY (profile_key, owner_id, followup_id) REFERENCES followups(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, evidence_id) REFERENCES evidence(profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS plans (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 followup_id TEXT NOT NULL, operation_id TEXT NOT NULL, operation_version TEXT NOT NULL,
 parameters_json TEXT NOT NULL, parameters_hash TEXT NOT NULL,
 effect_class TEXT NOT NULL CHECK(effect_class IN ('informational', 'mutation')),
 evidence_revision INTEGER NOT NULL CHECK(evidence_revision >= 1), plan_hash TEXT NOT NULL,
 permission_revision INTEGER NOT NULL CHECK(permission_revision >= 1),
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, plan_hash),
 FOREIGN KEY (profile_key, owner_id, followup_id) REFERENCES followups(profile_key, owner_id, id)
);

CREATE INDEX IF NOT EXISTS idx_followups_due
 ON followups(profile_key, owner_id, status, next_check_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_followups_active_subject
 ON followups(profile_key, owner_id, subject_key)
 WHERE status NOT IN ('completed', 'cancelled', 'expired');

-- pragma: foreign_keys=off
-- 0.3: reversibility classes (read | local | external) replace informational | mutation,
-- and proposals become user-facing suggestions bound to tasks, with predefined options.
CREATE TABLE plans_new (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 followup_id TEXT NOT NULL, operation_id TEXT NOT NULL, operation_version TEXT NOT NULL,
 parameters_json TEXT NOT NULL, parameters_hash TEXT NOT NULL,
 effect_class TEXT NOT NULL CHECK(effect_class IN ('read', 'local', 'external')),
 evidence_revision INTEGER NOT NULL CHECK(evidence_revision >= 1), plan_hash TEXT NOT NULL,
 permission_revision INTEGER NOT NULL CHECK(permission_revision >= 1), source_id TEXT,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, plan_hash),
 FOREIGN KEY (profile_key, owner_id, followup_id) REFERENCES followups(profile_key, owner_id, id)
);
INSERT INTO plans_new
 SELECT id, profile_key, owner_id, created_at, updated_at, revision, followup_id, operation_id,
        operation_version, parameters_json, parameters_hash,
        CASE effect_class WHEN 'mutation' THEN 'external' ELSE 'read' END,
        evidence_revision, plan_hash, permission_revision, source_id
   FROM plans;
DROP TABLE plans;
ALTER TABLE plans_new RENAME TO plans;
CREATE INDEX IF NOT EXISTS idx_plans_source ON plans(profile_key, owner_id, source_id, created_at);

CREATE TABLE runs_new (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 followup_id TEXT NOT NULL, plan_id TEXT NOT NULL, consent_id TEXT,
 effect_class TEXT NOT NULL CHECK(effect_class IN ('read', 'local', 'external')),
 status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'unknown', 'cancelled')),
 execution_key TEXT NOT NULL, lease_owner TEXT, lease_until TEXT, fencing_token INTEGER NOT NULL DEFAULT 0,
 heartbeat_at TEXT, attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0), started_at TEXT, finished_at TEXT,
 result_json TEXT NOT NULL, evidence_refs_json TEXT NOT NULL, provider_receipt_json TEXT,
 error_code TEXT,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, execution_key),
 FOREIGN KEY (profile_key, owner_id, followup_id) REFERENCES followups(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, plan_id) REFERENCES plans(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, consent_id) REFERENCES consents(profile_key, owner_id, id)
);
INSERT INTO runs_new
 SELECT id, profile_key, owner_id, created_at, updated_at, revision, followup_id, plan_id, consent_id,
        CASE effect_class WHEN 'mutation' THEN 'external' ELSE 'read' END,
        status, execution_key, lease_owner, lease_until, fencing_token, heartbeat_at, attempt,
        started_at, finished_at, result_json, evidence_refs_json, provider_receipt_json, error_code
   FROM runs;
DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;
CREATE INDEX IF NOT EXISTS idx_runs_lease ON runs(profile_key, owner_id, status, lease_until);
CREATE INDEX IF NOT EXISTS idx_runs_execution_status ON runs(profile_key, owner_id, execution_key, status);

CREATE TABLE proposals_new (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 followup_id TEXT, plan_id TEXT, destination_id TEXT, task_id TEXT, pattern_id TEXT, job_id TEXT,
 status TEXT NOT NULL CHECK(status IN ('draft', 'pending', 'sent', 'accepted', 'declined', 'snoozed', 'expired', 'cancelled', 'executing', 'succeeded', 'failed', 'unknown')),
 expires_at TEXT NOT NULL, priority TEXT NOT NULL CHECK(priority IN ('normal', 'low', 'deadline')),
 reason_text TEXT NOT NULL, suppression_key TEXT NOT NULL,
 action_class TEXT NOT NULL DEFAULT 'read' CHECK(action_class IN ('read', 'local', 'external')),
 operation_id TEXT, params_json TEXT NOT NULL DEFAULT '{}', fingerprint TEXT NOT NULL DEFAULT '',
 title TEXT NOT NULL DEFAULT '', options_json TEXT NOT NULL DEFAULT '[]',
 urgency TEXT NOT NULL DEFAULT 'normal' CHECK(urgency IN ('low', 'normal', 'high')),
 batch_key TEXT, response_json TEXT, responded_at TEXT, outbox_id TEXT, snoozed_until TEXT,
 PRIMARY KEY (profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, followup_id) REFERENCES followups(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, plan_id) REFERENCES plans(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, destination_id) REFERENCES destinations(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, task_id) REFERENCES tasks(profile_key, owner_id, id)
);
INSERT INTO proposals_new
 (id, profile_key, owner_id, created_at, updated_at, revision, followup_id, plan_id, destination_id,
  status, expires_at, priority, reason_text, suppression_key, action_class, fingerprint)
 SELECT id, profile_key, owner_id, created_at, updated_at, revision, followup_id, plan_id, destination_id,
        CASE status WHEN 'rejected' THEN 'declined' ELSE status END,
        expires_at, priority, reason_text, suppression_key, 'external', suppression_key
   FROM proposals;
DROP TABLE proposals;
ALTER TABLE proposals_new RENAME TO proposals;
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(profile_key, owner_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_proposals_task ON proposals(profile_key, owner_id, task_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_proposals_open_fingerprint
 ON proposals(profile_key, owner_id, fingerprint)
 WHERE status IN ('draft', 'pending', 'sent', 'snoozed') AND fingerprint <> '';

-- pragma: foreign_keys=off
-- 0.3.1: proposals gain the 'waiting' status: approved by the owner (or chosen automatically) but not
-- started yet because another job runs on the task, the daily job budget is used up or jobs are off.
-- The scheduler resumes them every tick; they never sit as 'accepted' without a job again.
CREATE TABLE proposals_new (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 followup_id TEXT, plan_id TEXT, destination_id TEXT, task_id TEXT, pattern_id TEXT, job_id TEXT,
 status TEXT NOT NULL CHECK(status IN ('draft', 'pending', 'sent', 'accepted', 'waiting', 'declined', 'snoozed', 'expired', 'cancelled', 'executing', 'succeeded', 'failed', 'unknown')),
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
 (id, profile_key, owner_id, created_at, updated_at, revision, followup_id, plan_id, destination_id, task_id, pattern_id, job_id,
  status, expires_at, priority, reason_text, suppression_key, action_class, operation_id, params_json, fingerprint,
  title, options_json, urgency, batch_key, response_json, responded_at, outbox_id, snoozed_until)
 SELECT id, profile_key, owner_id, created_at, updated_at, revision, followup_id, plan_id, destination_id, task_id, pattern_id, job_id,
        status, expires_at, priority, reason_text, suppression_key, action_class, operation_id, params_json, fingerprint,
        title, options_json, urgency, batch_key, response_json, responded_at, outbox_id, snoozed_until
   FROM proposals;
DROP TABLE proposals;
ALTER TABLE proposals_new RENAME TO proposals;
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(profile_key, owner_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_proposals_task ON proposals(profile_key, owner_id, task_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_proposals_open_fingerprint
 ON proposals(profile_key, owner_id, fingerprint)
 WHERE status IN ('draft', 'pending', 'sent', 'snoozed', 'waiting') AND fingerprint <> '';

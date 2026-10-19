-- 0.3: delegated jobs, user facts, attention preferences, task links on timers and outbox.
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 task_id TEXT, proposal_id TEXT, kind TEXT NOT NULL, prompt TEXT NOT NULL,
 workdir TEXT, worktree INTEGER NOT NULL DEFAULT 0 CHECK(worktree IN (0, 1)),
 skills_json TEXT NOT NULL DEFAULT '[]', toolsets_json TEXT NOT NULL DEFAULT '[]',
 action_class TEXT NOT NULL CHECK(action_class IN ('read', 'local', 'external')),
 status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled', 'unknown')),
 pid INTEGER, session_ref TEXT, result_summary TEXT, result_json TEXT NOT NULL DEFAULT '{}',
 usage_json TEXT NOT NULL DEFAULT '{}', lease_until TEXT, fencing_token INTEGER NOT NULL DEFAULT 0,
 attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0), started_at TEXT, finished_at TEXT, error TEXT,
 PRIMARY KEY (profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, task_id) REFERENCES tasks(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, proposal_id) REFERENCES proposals(profile_key, owner_id, id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(profile_key, owner_id, status, created_at);

CREATE TABLE IF NOT EXISTS facts (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 key TEXT NOT NULL, value TEXT NOT NULL,
 confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence >= 0 AND confidence <= 1),
 provenance_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, key)
);

CREATE TABLE IF NOT EXISTS preferences (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 version INTEGER NOT NULL CHECK(version >= 1),
 dimension TEXT NOT NULL CHECK(dimension IN ('notification', 'investigation', 'response')),
 scope_json TEXT NOT NULL DEFAULT '{}',
 effect TEXT NOT NULL CHECK(effect IN ('silent', 'digest', 'notify', 'prefer')),
 exceptions_json TEXT NOT NULL DEFAULT '[]', instruction TEXT NOT NULL,
 origin TEXT NOT NULL CHECK(origin IN ('explicit', 'inferred', 'imported')),
 status TEXT NOT NULL CHECK(status IN ('active', 'candidate', 'disabled')),
 feedback_ref TEXT,
 PRIMARY KEY (profile_key, owner_id, id, version)
);

ALTER TABLE followups ADD COLUMN task_id TEXT;
ALTER TABLE sources ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE sources ADD COLUMN health_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE outbox ADD COLUMN task_id TEXT;
ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'notice';
CREATE INDEX IF NOT EXISTS idx_outbox_platform_message
 ON outbox(profile_key, owner_id, platform_message_id);
CREATE INDEX IF NOT EXISTS idx_followups_task ON followups(profile_key, owner_id, task_id);

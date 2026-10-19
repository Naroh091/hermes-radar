-- 0.3: normalized signal log, long-lived tasks and their links.
CREATE TABLE IF NOT EXISTS signals (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 source_id TEXT NOT NULL, kind TEXT NOT NULL,
 occurred_at TEXT NOT NULL, external_ref TEXT NOT NULL, group_key TEXT NOT NULL,
 title TEXT NOT NULL DEFAULT '', snippet TEXT NOT NULL DEFAULT '',
 meta_json TEXT NOT NULL DEFAULT '{}', extract_json TEXT,
 content_ref TEXT, raw_content TEXT, raw_expires_at TEXT,
 fingerprint TEXT NOT NULL,
 triage_status TEXT NOT NULL CHECK(triage_status IN ('pending', 'ignored', 'extracted', 'error', 'uncertain')),
 triage_reason TEXT, attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
 next_attempt_at TEXT NOT NULL, lease_until TEXT, fencing_token INTEGER NOT NULL DEFAULT 0,
 processed_at TEXT, expires_at TEXT,
 PRIMARY KEY (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, source_id, external_ref),
 FOREIGN KEY (profile_key, owner_id, source_id) REFERENCES sources(profile_key, owner_id, id)
);
CREATE INDEX IF NOT EXISTS idx_signals_pending
 ON signals(profile_key, owner_id, triage_status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_signals_group
 ON signals(profile_key, owner_id, group_key, created_at);
CREATE INDEX IF NOT EXISTS idx_signals_fingerprint
 ON signals(profile_key, owner_id, fingerprint);

CREATE TABLE IF NOT EXISTS tasks (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 kind TEXT NOT NULL CHECK(kind IN ('trip', 'purchase', 'work_item', 'commitment', 'check', 'routine', 'other')),
 title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('open', 'waiting', 'needs_user', 'in_progress', 'snoozed', 'done', 'abandoned', 'expired')),
 entities_json TEXT NOT NULL DEFAULT '{}', expectations_json TEXT NOT NULL DEFAULT '[]',
 checklist_json TEXT NOT NULL DEFAULT '[]',
 next_review_at TEXT, due_at TEXT, snoozed_until TEXT,
 confidence REAL NOT NULL DEFAULT 0.5, origin_signal_id TEXT, closed_reason TEXT,
 muted INTEGER NOT NULL DEFAULT 0 CHECK(muted IN (0, 1)),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 PRIMARY KEY (profile_key, owner_id, id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status
 ON tasks(profile_key, owner_id, status, next_review_at);

CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
 task_id UNINDEXED, profile_key UNINDEXED, owner_id UNINDEXED, title, summary, entities
);

CREATE TABLE IF NOT EXISTS task_signals (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 task_id TEXT NOT NULL, signal_id TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('origin', 'advances', 'completes', 'contradicts', 'context')),
 PRIMARY KEY (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, task_id, signal_id, role),
 FOREIGN KEY (profile_key, owner_id, task_id) REFERENCES tasks(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, signal_id) REFERENCES signals(profile_key, owner_id, id)
);

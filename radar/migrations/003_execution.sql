-- Compatibility backfill for databases created by the 0.1.0 bootstrap,
-- whose first two migrations did not yet contain the complete P1 tables.
CREATE TABLE IF NOT EXISTS grants (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
 permission TEXT NOT NULL, source_id TEXT, operation_ids_json TEXT NOT NULL,
 resource_scope_json TEXT NOT NULL, destination_id TEXT, limits_json TEXT NOT NULL,
 expires_at TEXT, revoked_at TEXT,
 PRIMARY KEY (profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, source_id) REFERENCES sources(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, destination_id) REFERENCES destinations(profile_key, owner_id, id)
);
CREATE TABLE IF NOT EXISTS evidence (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
 observation_id TEXT NOT NULL, source_ref TEXT NOT NULL, excerpt_redacted TEXT NOT NULL,
 epistemic_status TEXT NOT NULL, supersedes_id TEXT, expires_at TEXT,
 PRIMARY KEY (profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, observation_id) REFERENCES observations(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, supersedes_id) REFERENCES evidence(profile_key, owner_id, id)
);
CREATE TABLE IF NOT EXISTS followup_evidence (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
 followup_id TEXT NOT NULL, evidence_id TEXT NOT NULL, role TEXT NOT NULL,
 PRIMARY KEY (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, followup_id, evidence_id, role),
 FOREIGN KEY (profile_key, owner_id, followup_id) REFERENCES followups(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, evidence_id) REFERENCES evidence(profile_key, owner_id, id)
);
CREATE TABLE IF NOT EXISTS plans (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
 followup_id TEXT NOT NULL, operation_id TEXT NOT NULL, operation_version TEXT NOT NULL,
 parameters_json TEXT NOT NULL, parameters_hash TEXT NOT NULL, effect_class TEXT NOT NULL,
 evidence_revision INTEGER NOT NULL, plan_hash TEXT NOT NULL, permission_revision INTEGER NOT NULL,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, plan_hash),
 FOREIGN KEY (profile_key, owner_id, followup_id) REFERENCES followups(profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS proposals (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 followup_id TEXT NOT NULL, plan_id TEXT NOT NULL, destination_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft', 'pending', 'accepted', 'rejected', 'expired', 'cancelled', 'executing', 'succeeded', 'failed', 'unknown')),
 expires_at TEXT NOT NULL, priority TEXT NOT NULL CHECK(priority IN ('normal', 'low', 'deadline')),
 reason_text TEXT NOT NULL, suppression_key TEXT NOT NULL,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, followup_id) REFERENCES followups(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, plan_id) REFERENCES plans(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, destination_id) REFERENCES destinations(profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS consents (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 proposal_id TEXT NOT NULL, plan_hash TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
 actor_ref TEXT NOT NULL, verified_transport TEXT NOT NULL, destination_id TEXT NOT NULL,
 expires_at TEXT NOT NULL, token_hash TEXT NOT NULL, consumed_at TEXT, revoked_at TEXT,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, token_hash),
 FOREIGN KEY (profile_key, owner_id, proposal_id) REFERENCES proposals(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, destination_id) REFERENCES destinations(profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS runs (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 followup_id TEXT NOT NULL, plan_id TEXT NOT NULL, consent_id TEXT,
 effect_class TEXT NOT NULL CHECK(effect_class IN ('informational', 'mutation')),
 status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'unknown', 'cancelled')),
 execution_key TEXT NOT NULL, lease_owner TEXT, lease_until TEXT, fencing_token INTEGER NOT NULL DEFAULT 0,
 heartbeat_at TEXT, attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0), started_at TEXT, finished_at TEXT,
 result_json TEXT NOT NULL, evidence_refs_json TEXT NOT NULL, provider_receipt_json TEXT,
 error_code TEXT,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, execution_key),
 FOREIGN KEY (profile_key, owner_id, followup_id) REFERENCES followups(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, plan_id) REFERENCES plans(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, consent_id) REFERENCES consents(profile_key, owner_id, id)
);

CREATE INDEX IF NOT EXISTS idx_proposals_status
 ON proposals(profile_key, owner_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_runs_lease
 ON runs(profile_key, owner_id, status, lease_until);

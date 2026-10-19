CREATE TABLE IF NOT EXISTS destinations (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 platform TEXT NOT NULL, gateway_connection_ref TEXT NOT NULL, address_json TEXT NOT NULL,
 owner_platform_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('private', 'group')),
 capabilities_json TEXT NOT NULL, verified_at TEXT NOT NULL,
 enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id)
);

CREATE TABLE IF NOT EXISTS outbox (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 destination_id TEXT NOT NULL, followup_id TEXT, proposal_id TEXT,
 notification_key TEXT NOT NULL, payload_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'sent', 'failed', 'unknown', 'cancelled')),
 attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0), next_attempt_at TEXT NOT NULL,
 lease_until TEXT, fencing_token INTEGER NOT NULL DEFAULT 0,
 platform_message_id TEXT, last_error TEXT,
 PRIMARY KEY (profile_key, owner_id, id), UNIQUE (profile_key, owner_id, id),
 UNIQUE (profile_key, owner_id, notification_key),
 FOREIGN KEY (profile_key, owner_id, destination_id) REFERENCES destinations(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, followup_id) REFERENCES followups(profile_key, owner_id, id),
 FOREIGN KEY (profile_key, owner_id, proposal_id) REFERENCES proposals(profile_key, owner_id, id)
);

CREATE INDEX IF NOT EXISTS idx_outbox_due
 ON outbox(profile_key, owner_id, status, next_attempt_at);

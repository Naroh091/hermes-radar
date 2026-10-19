CREATE TABLE IF NOT EXISTS card_kinds (
 id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 label_json TEXT NOT NULL DEFAULT '{}', icon TEXT NOT NULL DEFAULT '', hue INTEGER NOT NULL DEFAULT 0 CHECK(hue >= 0 AND hue <= 360),
 layout TEXT NOT NULL, spec_json TEXT NOT NULL DEFAULT '{}', origin TEXT NOT NULL CHECK(origin IN ('builtin', 'generated')),
 status TEXT NOT NULL CHECK(status IN ('active', 'disabled')), support INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY (profile_key, owner_id, id)
);

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Scope:
    def __init__(self, profile_key: str, owner_id: str):
        if not profile_key or not owner_id: raise ValueError("profile_key and owner_id are required")
        self.profile_key, self.owner_id = profile_key, owner_id


class Store:
    """SQLite aislado del host; cada operación recibe scope explícito."""
    def __init__(self, path: str | os.PathLike[str], scope: Scope):
        self.path, self.scope = Path(path), scope
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try: os.chmod(self.path.parent, 0o700)
        except OSError: pass
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        try: os.chmod(self.path, 0o600)
        except OSError: pass

    def _migrate(self):
        self.db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        current = self.db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        root = Path(__file__).parent / "migrations"
        files = sorted(root.glob("*.sql"))
        for number, migration in enumerate(files, 1):
            if number <= current: continue
            script = migration.read_text(encoding="utf-8")
            # executescript() commits any open transaction before running. Put the
            # migration and version marker in the same explicit script instead.
            self.db.executescript(
                "BEGIN;\n" + script + "\nINSERT INTO schema_version(version) VALUES (" + str(number) + ");\nCOMMIT;"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def status(self) -> dict:
        tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'schema_version'").fetchall()
        counts = {row[0]: self.db.execute(f"SELECT COUNT(*) FROM {row[0]}").fetchone()[0] for row in tables}
        version = self.db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        return {"schema_version": version, "scope": vars(self.scope), "counts": counts}

    def add_goal(self, goal_id: str, title: str, now: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO goals VALUES (?,?,?,?,?,?,?,?,?,?,?)", (goal_id, self.scope.profile_key, self.scope.owner_id, now, now, 1, title, "active", "{}", None, None))

    def list_followups(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        rows = self.db.execute("SELECT id, goal_id, reason, status, subject_key, next_check_at, confidence_band FROM followups WHERE profile_key=? AND owner_id=? ORDER BY updated_at DESC LIMIT ?", (self.scope.profile_key, self.scope.owner_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def close(self): self.db.close()

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .models import TASK_TERMINAL, PlanIdentity, canonical_json, validate_transition


@dataclass(frozen=True)
class Scope:
    profile_key: str
    owner_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.profile_key, str) or not self.profile_key.strip():
            raise ValueError("profile_key is required")
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise ValueError("owner_id is required")


_FK_OFF_MARKER = "-- pragma: foreign_keys=off"
_FTS_TOKEN = re.compile(r"[0-9A-Za-zÀ-ÿ_]{3,}")


class Store:
    """SQLite owned by the plugin; every repository operation is scope-bound.

    Connections are per thread: the scheduler runs on the gateway loop while hook
    callbacks run on agent worker threads, and SQLite connections must not cross threads.
    """

    migration_dir = Path(__file__).parent / "migrations"

    def __init__(self, path: str | os.PathLike[str], scope: Scope):
        self.path = Path(path)
        self.scope = scope
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self._closed = False
        self._migrate()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- connections -------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        with self._conn_lock:
            self._connections.append(db)
        return db

    @property
    def db(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("store is closed")
        db = getattr(self._local, "db", None)
        if db is None:
            db = self._connect()
            self._local.db = db
        return db

    @property
    def _scope_values(self) -> tuple[str, str]:
        return self.scope.profile_key, self.scope.owner_id

    def _migrate(self) -> None:
        db = self.db
        db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        current = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        files = sorted(self.migration_dir.glob("*.sql"))
        for number, migration in enumerate(files, 1):
            if number <= current:
                continue
            script = migration.read_text(encoding="utf-8")
            relax_fk = _FK_OFF_MARKER in script
            if relax_fk:
                db.execute("PRAGMA foreign_keys=OFF")
            try:
                db.executescript("BEGIN IMMEDIATE;\n" + script)
                if relax_fk:
                    problems = db.execute("PRAGMA foreign_key_check").fetchall()
                    if problems:
                        raise sqlite3.IntegrityError(
                            f"foreign key check failed after migration {migration.name}: {len(problems)} rows"
                        )
                db.execute("INSERT INTO schema_version(version) VALUES (?)", (number,))
                db.execute("COMMIT")
            except sqlite3.DatabaseError:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                if relax_fk:
                    db.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    @staticmethod
    def _object(value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be an object")
        canonical_json(value)
        return value

    @staticmethod
    def _array(value: Any, field: str) -> list[Any]:
        if not isinstance(value, list):
            raise ValueError(f"{field} must be an array")
        canonical_json(value)
        return value

    @staticmethod
    def _string_list(value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            raise ValueError(f"{field} must be an array of non-empty strings")
        return value

    @staticmethod
    def _decode(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in list(result):
            if key.endswith("_json") and isinstance(result[key], str):
                try:
                    result[key[:-5]] = json.loads(result.pop(key))
                except json.JSONDecodeError:
                    pass
        return result

    def status(self) -> dict[str, Any]:
        tables = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'schema_version' "
            "AND name NOT LIKE '%\\_fts%' ESCAPE '\\' ORDER BY name"
        ).fetchall()
        counts = {
            row[0]: self.db.execute(
                f'SELECT COUNT(*) FROM "{row[0]}" WHERE profile_key=? AND owner_id=?', self._scope_values
            ).fetchone()[0]
            for row in tables
        }
        version = self.db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        return {"schema_version": version, "scope": vars(self.scope), "counts": counts}

    # -- sources and grants ---------------------------------------------------------------
    def add_source(
        self,
        *,
        source_id: str,
        kind: str,
        account_ref: str,
        resource_scope: dict[str, Any],
        retention_days: int,
        now: str,
        enabled: bool = True,
        permission_revision: int = 1,
        cursor: dict[str, Any] | None = None,
    ) -> None:
        resource_scope = self._object(resource_scope, "resource_scope")
        cursor = self._object(cursor or {}, "cursor")
        if retention_days < 1 or permission_revision < 1:
            raise ValueError("retention_days and permission_revision must be positive")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO sources
                (id, profile_key, owner_id, created_at, updated_at, revision, kind, account_ref,
                 resource_scope_json, enabled, permission_revision, cursor_json, last_success_at, retention_days)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    source_id, *self._scope_values, now, now, kind, account_ref,
                    canonical_json(resource_scope), int(enabled), permission_revision,
                    canonical_json(cursor), retention_days,
                ),
            )

    def add_grant(
        self,
        *,
        grant_id: str,
        permission: str,
        source_id: str | None,
        operation_ids: list[str],
        resource_scope: dict[str, Any],
        destination_id: str | None,
        limits: dict[str, Any],
        expires_at: str | None,
        now: str,
    ) -> None:
        operation_ids = self._string_list(operation_ids, "operation_ids")
        resource_scope = self._object(resource_scope, "resource_scope")
        limits = self._object(limits, "limits")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO grants
                (id, profile_key, owner_id, created_at, updated_at, revision, permission, source_id,
                 operation_ids_json, resource_scope_json, destination_id, limits_json, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    grant_id, *self._scope_values, now, now, permission, source_id,
                    canonical_json(operation_ids), canonical_json(resource_scope), destination_id,
                    canonical_json(limits), expires_at,
                ),
            )

    def active_operation_grants(self, operation_id: str, source_id: str, now: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT g.id, g.operation_ids_json, g.resource_scope_json, g.limits_json,
                      s.resource_scope_json AS source_resource_scope_json,
                      s.permission_revision
               FROM grants g
               JOIN sources s
                 ON s.profile_key=g.profile_key AND s.owner_id=g.owner_id AND s.id=g.source_id
              WHERE g.profile_key=? AND g.owner_id=? AND g.source_id=?
                AND g.permission='read' AND g.revoked_at IS NULL
                AND (g.expires_at IS NULL OR g.expires_at>?) AND s.enabled=1""",
            (*self._scope_values, source_id, now),
        ).fetchall()
        active: list[dict[str, Any]] = []
        for row in rows:
            try:
                operation_ids = json.loads(row["operation_ids_json"])
                grant_scope = json.loads(row["resource_scope_json"])
                source_scope = json.loads(row["source_resource_scope_json"])
                limits = json.loads(row["limits_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                not isinstance(operation_ids, list)
                or operation_id not in operation_ids
                or not isinstance(grant_scope, dict)
                or not isinstance(source_scope, dict)
                or not isinstance(limits, dict)
            ):
                continue
            active.append(
                {
                    "grant_id": row["id"],
                    "resource_scope": grant_scope,
                    "source_resource_scope": source_scope,
                    "limits": limits,
                    "permission_revision": row["permission_revision"],
                }
            )
        return active

    def has_operation_grant(self, operation_id: str, source_id: str, now: str) -> bool:
        return bool(self.active_operation_grants(operation_id, source_id, now))

    def revoke_grant(self, grant_id: str, now: str) -> None:
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE grants SET revoked_at=?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND revoked_at IS NULL""",
                (now, now, *self._scope_values, grant_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(grant_id)

    def sync_source(
        self,
        *,
        source_id: str,
        kind: str,
        account_ref: str,
        config: dict[str, Any],
        resource_scope: dict[str, Any],
        retention_days: int,
        enabled: bool,
        operations: list[str],
        now: str,
    ) -> dict[str, str]:
        """Idempotently materialize one configured non-HTTP source and its read grant.

        The cursor is preserved across syncs so a config edit never replays history.
        """
        if retention_days < 1:
            raise ValueError("source retention must be positive")
        config = self._object(config, "config")
        resource_scope = self._object(resource_scope, "resource_scope")
        source_key = f"configured:{source_id}"
        grant_id = f"configured:{source_id}:read"
        with self.transaction() as db:
            db.execute(
                """INSERT INTO sources
                (id, profile_key, owner_id, created_at, updated_at, revision, kind, account_ref,
                 resource_scope_json, enabled, permission_revision, cursor_json, last_success_at,
                 retention_days, config_json, health_json)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 1, '{}', NULL, ?, ?, '{}')
                ON CONFLICT(profile_key, owner_id, id) DO UPDATE SET
                  updated_at=excluded.updated_at, revision=sources.revision+1, kind=excluded.kind,
                  account_ref=excluded.account_ref, resource_scope_json=excluded.resource_scope_json,
                  enabled=excluded.enabled, retention_days=excluded.retention_days,
                  config_json=excluded.config_json,
                  permission_revision=sources.permission_revision +
                    CASE WHEN sources.resource_scope_json<>excluded.resource_scope_json THEN 1 ELSE 0 END""",
                (source_key, *self._scope_values, now, now, kind, account_ref,
                 canonical_json(resource_scope), int(enabled), retention_days, canonical_json(config)),
            )
            db.execute(
                """INSERT INTO grants
                (id, profile_key, owner_id, created_at, updated_at, revision, permission, source_id,
                 operation_ids_json, resource_scope_json, destination_id, limits_json, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, 1, 'read', ?, ?, ?, NULL, '{}', NULL, NULL)
                ON CONFLICT(profile_key, owner_id, id) DO UPDATE SET
                  updated_at=excluded.updated_at, revision=grants.revision+1,
                  operation_ids_json=excluded.operation_ids_json,
                  resource_scope_json=excluded.resource_scope_json, revoked_at=NULL""",
                (grant_id, *self._scope_values, now, now, source_key,
                 canonical_json(self._string_list(operations, "operations")), canonical_json(resource_scope)),
            )
        return {"source_id": source_key, "grant_id": grant_id}

    def get_source(self, source_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM sources WHERE profile_key=? AND owner_id=? AND id=?",
            (*self._scope_values, source_id),
        ).fetchone()
        if row is None:
            raise KeyError(source_id)
        return self._decode(row) or {}

    def list_sources(self, kind: str | None = None, enabled_only: bool = True) -> list[dict[str, Any]]:
        clauses = ["profile_key=?", "owner_id=?"]
        params: list[Any] = list(self._scope_values)
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if enabled_only:
            clauses.append("enabled=1")
        rows = self.db.execute(
            f"SELECT * FROM sources WHERE {' AND '.join(clauses)} ORDER BY id", params
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def update_source_cursor(self, source_id: str, cursor: dict[str, Any], now: str, *, success: bool = True) -> None:
        cursor = self._object(cursor, "cursor")
        with self.transaction() as db:
            db.execute(
                """UPDATE sources SET cursor_json=?, updated_at=?, revision=revision+1,
                          last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END
                   WHERE profile_key=? AND owner_id=? AND id=?""",
                (canonical_json(cursor), now, int(success), now, *self._scope_values, source_id),
            )

    def update_source_health(self, source_id: str, health: dict[str, Any], now: str) -> None:
        health = self._object(health, "health")
        with self.transaction() as db:
            db.execute(
                """UPDATE sources SET health_json=?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=?""",
                (canonical_json(health), now, *self._scope_values, source_id),
            )

    # -- goals and follow-ups -------------------------------------------------------------
    def add_goal(self, goal_id: str, title: str, now: str) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO goals
                (id, profile_key, owner_id, created_at, updated_at, revision, title, status,
                 scope_json, user_intent_ref, parent_goal_id)
                VALUES (?, ?, ?, ?, ?, 1, ?, 'active', '{}', NULL, NULL)""",
                (goal_id, *self._scope_values, now, now, title),
            )

    def ensure_goal(self, goal_id: str, title: str, now: str) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO goals
                (id, profile_key, owner_id, created_at, updated_at, revision, title, status,
                 scope_json, user_intent_ref, parent_goal_id)
                VALUES (?, ?, ?, ?, ?, 1, ?, 'active', '{}', NULL, NULL)""",
                (goal_id, *self._scope_values, now, now, title),
            )

    def list_goals(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        rows = self.db.execute(
            """SELECT id, title, status, revision, created_at, updated_at
               FROM goals WHERE profile_key=? AND owner_id=?
               ORDER BY updated_at DESC, id LIMIT ?""",
            (*self._scope_values, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_goal(self, goal_id: str) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT id, title, status, scope_json, user_intent_ref, parent_goal_id, revision,
                      created_at, updated_at
               FROM goals WHERE profile_key=? AND owner_id=? AND id=?""",
            (*self._scope_values, goal_id),
        ).fetchone()
        if row is None:
            raise KeyError(goal_id)
        return dict(row)

    def add_followup(
        self,
        *,
        followup_id: str,
        goal_id: str,
        reason: str,
        status: str,
        subject_key: str,
        evidence_revision: int,
        expires_at: str,
        timezone: str,
        confidence_band: str,
        assumptions: dict[str, Any],
        stop_rule: dict[str, Any],
        now: str,
        next_check_at: str | None = None,
        deadline_at: str | None = None,
        task_id: str | None = None,
    ) -> None:
        assumptions = self._object(assumptions, "assumptions")
        stop_rule = self._object(stop_rule, "stop_rule")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO followups
                (id, profile_key, owner_id, created_at, updated_at, revision, goal_id, reason, status,
                 subject_key, evidence_revision, next_check_at, deadline_at, expires_at, timezone,
                 confidence_band, assumptions_json, stop_rule_json, last_notified_at, reminder_count, task_id)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)""",
                (
                    followup_id, *self._scope_values, now, now, goal_id, reason, status, subject_key,
                    evidence_revision, next_check_at, deadline_at, expires_at, timezone, confidence_band,
                    canonical_json(assumptions), canonical_json(stop_rule), task_id,
                ),
            )

    def upsert_followup(
        self,
        *,
        followup_id: str,
        goal_id: str,
        goal_title: str,
        reason: str,
        subject_key: str,
        next_check_at: str | None,
        expires_at: str,
        assumptions: dict[str, Any],
        now: str,
        task_id: str | None = None,
        status: str = "ready",
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        """Create or re-arm a scheduler timer (task reviews, routines). Terminal rows stay closed."""
        assumptions = self._object(assumptions, "assumptions")
        self.ensure_goal(goal_id, goal_title, now)
        with self.transaction() as db:
            db.execute(
                """INSERT INTO followups
                (id, profile_key, owner_id, created_at, updated_at, revision, goal_id, reason, status,
                 subject_key, evidence_revision, next_check_at, deadline_at, expires_at, timezone,
                 confidence_band, assumptions_json, stop_rule_json, last_notified_at, reminder_count, task_id)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 1, ?, NULL, ?, ?, 'high', ?, '{}', NULL, 0, ?)
                ON CONFLICT(profile_key, owner_id, id) DO UPDATE SET
                  updated_at=excluded.updated_at, revision=followups.revision+1,
                  next_check_at=excluded.next_check_at, expires_at=excluded.expires_at,
                  assumptions_json=excluded.assumptions_json, task_id=excluded.task_id,
                  status=CASE WHEN followups.status IN ('completed','cancelled','expired') THEN followups.status
                              WHEN followups.status='checking' THEN 'checking'
                              ELSE excluded.status END""",
                (followup_id, *self._scope_values, now, now, goal_id, reason, status, subject_key,
                 next_check_at, expires_at, timezone, canonical_json(assumptions), task_id),
            )
        return {"id": followup_id}

    def cancel_task_followups(self, task_id: str, now: str) -> int:
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE followups SET status='cancelled', updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND task_id=?
                     AND status NOT IN ('completed','cancelled','expired')""",
                (now, *self._scope_values, task_id),
            )
        return cursor.rowcount

    def add_plan(
        self,
        *,
        plan_id: str,
        followup_id: str,
        operation_id: str,
        operation_version: str,
        parameters: dict[str, Any],
        effect_class: str,
        evidence_revision: int,
        permission_revision: int,
        now: str,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        parameters = self._object(parameters, "parameters")
        identity = PlanIdentity(
            followup_id=followup_id,
            operation_id=operation_id,
            operation_version=operation_version,
            parameters=parameters,
            effect_class=effect_class,
            evidence_revision=evidence_revision,
            permission_revision=permission_revision,
        )
        with self.transaction() as db:
            existing = db.execute(
                """SELECT id, plan_hash, parameters_hash FROM plans
                   WHERE profile_key=? AND owner_id=? AND plan_hash=?""",
                (*self._scope_values, identity.plan_hash),
            ).fetchone()
            if existing is not None:
                result = dict(existing)
                result["created"] = False
                return result
            db.execute(
                """INSERT INTO plans
                (id, profile_key, owner_id, created_at, updated_at, revision, followup_id,
                 operation_id, operation_version, parameters_json, parameters_hash, effect_class,
                 evidence_revision, plan_hash, permission_revision, source_id)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan_id, *self._scope_values, now, now, followup_id, operation_id, operation_version,
                    canonical_json(parameters), identity.parameters_hash, effect_class, evidence_revision,
                    identity.plan_hash, permission_revision, source_id,
                ),
            )
        return {
            "id": plan_id,
            "plan_hash": identity.plan_hash,
            "parameters_hash": identity.parameters_hash,
            "created": True,
        }

    def get_followup(self, followup_id: str) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT id, goal_id, reason, status, subject_key, evidence_revision, next_check_at,
                      expires_at, revision, created_at, updated_at, task_id, assumptions_json
               FROM followups WHERE profile_key=? AND owner_id=? AND id=?""",
            (*self._scope_values, followup_id),
        ).fetchone()
        if row is None:
            raise KeyError(followup_id)
        return self._decode(row) or {}

    def list_followups(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        rows = self.db.execute(
            """SELECT id, goal_id, reason, status, subject_key, next_check_at, confidence_band, task_id
               FROM followups WHERE profile_key=? AND owner_id=?
               ORDER BY updated_at DESC, id LIMIT ?""",
            (*self._scope_values, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def transition_state(
        self,
        machine: str,
        object_id: str,
        current: str,
        target: str,
        expected_revision: int,
        now: str,
    ) -> int:
        tables = {
            "followup": "followups", "proposal": "proposals", "run": "runs", "outbox": "outbox",
            "task": "tasks", "job": "jobs",
        }
        table = tables.get(machine)
        if table is None:
            validate_transition(machine, current, target)
            raise AssertionError("unreachable")
        validate_transition(machine, current, target)
        row = self.db.execute(
            f'SELECT status, revision FROM "{table}" WHERE profile_key=? AND owner_id=? AND id=?',
            (*self._scope_values, object_id),
        ).fetchone()
        if row is None:
            raise KeyError(object_id)
        if row["status"] != current:
            raise RuntimeError(f"state conflict: expected {current}, found {row['status']}")
        if row["revision"] != expected_revision:
            raise RuntimeError(f"revision conflict: expected {expected_revision}, found {row['revision']}")
        with self.transaction() as db:
            cursor = db.execute(
                f'''UPDATE "{table}" SET status=?, updated_at=?, revision=revision+1
                    WHERE profile_key=? AND owner_id=? AND id=? AND status=? AND revision=?''',
                (target, now, *self._scope_values, object_id, current, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("concurrent state transition")
        return expected_revision + 1

    def sync_http_source(
        self,
        *,
        source_id: str,
        url: str,
        host: str,
        interval_seconds: int,
        retention_days: int,
        enabled: bool,
        allowed_operations: list[str],
        now: str,
    ) -> dict[str, str]:
        """Idempotently materialize one configured HTTPS monitor and its read grant."""
        if interval_seconds < 1 or retention_days < 1:
            raise ValueError("source interval and retention must be positive")
        resource_scope = {"hosts": [host]}
        source_key = f"configured:{source_id}"
        grant_id = f"configured:{source_id}:read"
        goal_id = f"configured:{source_id}:goal"
        followup_id = f"configured:{source_id}:followup"
        assumptions = {
            "kind": "operation",
            "source_id": source_key,
            "operation_id": "http_status.check",
            "parameters": {"url": url},
            "interval_seconds": interval_seconds,
        }
        with self.transaction() as db:
            db.execute(
                """INSERT INTO sources
                (id, profile_key, owner_id, created_at, updated_at, revision, kind, account_ref,
                 resource_scope_json, enabled, permission_revision, cursor_json, last_success_at, retention_days)
                VALUES (?, ?, ?, ?, ?, 1, 'http_status', ?, ?, ?, 1, '{}', NULL, ?)
                ON CONFLICT(profile_key, owner_id, id) DO UPDATE SET
                  updated_at=excluded.updated_at, revision=sources.revision+1,
                  account_ref=excluded.account_ref, resource_scope_json=excluded.resource_scope_json,
                  enabled=excluded.enabled, retention_days=excluded.retention_days,
                  permission_revision=sources.permission_revision +
                    CASE WHEN sources.resource_scope_json<>excluded.resource_scope_json THEN 1 ELSE 0 END""",
                (source_key, *self._scope_values, now, now, url, canonical_json(resource_scope), int(enabled), retention_days),
            )
            db.execute(
                """INSERT INTO grants
                (id, profile_key, owner_id, created_at, updated_at, revision, permission, source_id,
                 operation_ids_json, resource_scope_json, destination_id, limits_json, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, 1, 'read', ?, ?, ?, NULL, '{}', NULL, NULL)
                ON CONFLICT(profile_key, owner_id, id) DO UPDATE SET
                  updated_at=excluded.updated_at, revision=grants.revision+1,
                  operation_ids_json=excluded.operation_ids_json,
                  resource_scope_json=excluded.resource_scope_json, revoked_at=NULL""",
                (grant_id, *self._scope_values, now, now, source_key, canonical_json(allowed_operations), canonical_json(resource_scope)),
            )
            db.execute(
                """INSERT INTO goals
                (id, profile_key, owner_id, created_at, updated_at, revision, title, status,
                 scope_json, user_intent_ref, parent_goal_id)
                VALUES (?, ?, ?, ?, ?, 1, ?, 'active', ?, ?, NULL)
                ON CONFLICT(profile_key, owner_id, id) DO UPDATE SET
                  updated_at=excluded.updated_at, revision=goals.revision+1,
                  title=excluded.title, scope_json=excluded.scope_json""",
                (goal_id, *self._scope_values, now, now, f"Monitor {host}", canonical_json(resource_scope), source_key),
            )
            db.execute(
                """INSERT INTO followups
                (id, profile_key, owner_id, created_at, updated_at, revision, goal_id, reason, status,
                 subject_key, evidence_revision, next_check_at, deadline_at, expires_at, timezone,
                 confidence_band, assumptions_json, stop_rule_json, last_notified_at, reminder_count)
                VALUES (?, ?, ?, ?, ?, 1, ?, 'external_signal', ?, ?, 1, ?, NULL, '9999-12-31T23:59:59Z',
                        'UTC', 'high', ?, '{}', NULL, 0)
                ON CONFLICT(profile_key, owner_id, id) DO UPDATE SET
                  updated_at=excluded.updated_at, revision=followups.revision+1,
                  status=CASE WHEN excluded.status='paused' THEN 'paused'
                              WHEN followups.status IN ('completed','cancelled','expired') THEN followups.status
                              WHEN followups.status IN ('paused','failed') THEN 'ready'
                              ELSE followups.status END,
                  assumptions_json=excluded.assumptions_json""",
                (followup_id, *self._scope_values, now, now, goal_id,
                 "ready" if enabled else "paused", f"http:{host}", now, canonical_json(assumptions)),
            )
        return {"source_id": source_key, "grant_id": grant_id, "goal_id": goal_id, "followup_id": followup_id}

    def disable_missing_configured_sources(self, active_source_ids: set[str], now: str) -> int:
        """Fail closed when a previously materialized source disappears from configuration."""
        with self.transaction() as db:
            rows = db.execute(
                """SELECT id FROM sources
                   WHERE profile_key=? AND owner_id=? AND id LIKE 'configured:%' AND enabled=1""",
                self._scope_values,
            ).fetchall()
            inactive = [row["id"] for row in rows if row["id"] not in active_source_ids]
            for source_id in inactive:
                suffix = source_id.removeprefix("configured:")
                db.execute(
                    """UPDATE sources SET enabled=0, updated_at=?, revision=revision+1,
                              permission_revision=permission_revision+1
                       WHERE profile_key=? AND owner_id=? AND id=?""",
                    (now, *self._scope_values, source_id),
                )
                db.execute(
                    """UPDATE grants SET revoked_at=COALESCE(revoked_at, ?), updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND source_id=?""",
                    (now, now, *self._scope_values, source_id),
                )
                db.execute(
                    """UPDATE followups SET status='paused', updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND id=?
                         AND status NOT IN ('completed','cancelled','expired')""",
                    (now, *self._scope_values, f"configured:{suffix}:followup"),
                )
        return len(inactive)

    def ensure_owner_source(self, now: str) -> str:
        """The source of the owner's own answers (a button pressed, a written yes or no), so they can travel
        through the pipeline like any other signal and re-plan the task they answer."""
        source_id = "local:owner"
        with self.transaction() as db:
            db.execute(
                """INSERT INTO sources
                (id, profile_key, owner_id, created_at, updated_at, revision, kind, account_ref,
                 resource_scope_json, enabled, permission_revision, cursor_json, last_success_at, retention_days)
                VALUES (?, ?, ?, ?, ?, 1, 'owner', 'owner', '{}', 1, 1, '{}', NULL, 365)
                ON CONFLICT(profile_key, owner_id, id) DO NOTHING""",
                (source_id, *self._scope_values, now, now),
            )
        return source_id

    def ensure_local_observation_source(self, now: str, retention_days: int) -> str:
        source_id = "local:session-hooks"
        with self.transaction() as db:
            db.execute(
                """INSERT INTO sources
                (id, profile_key, owner_id, created_at, updated_at, revision, kind, account_ref,
                 resource_scope_json, enabled, permission_revision, cursor_json, last_success_at, retention_days)
                VALUES (?, ?, ?, ?, ?, 1, 'session', 'hermes-hooks', '{}', 1, 1, '{}', NULL, ?)
                ON CONFLICT(profile_key, owner_id, id) DO UPDATE SET
                  updated_at=excluded.updated_at, revision=sources.revision+1,
                  enabled=1, retention_days=excluded.retention_days""",
                (source_id, *self._scope_values, now, now, retention_days),
            )
        return source_id

    def add_observation(
        self, *, observation_id: str, source_id: str, source_event_key: str, occurred_at: str,
        received_at: str, kind: str, fingerprint: str, facts: dict[str, Any], origin: str,
        expires_at: str | None = None, correlation_id: str | None = None,
    ) -> bool:
        with self.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO observations
                (id, profile_key, owner_id, created_at, updated_at, revision, source_id,
                 source_event_key, occurred_at, received_at, kind, fingerprint, facts_json,
                 expires_at, origin, correlation_id)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (observation_id, *self._scope_values, received_at, received_at, source_id,
                 source_event_key, occurred_at, received_at, kind, fingerprint,
                 canonical_json(self._object(facts, "facts")), expires_at, origin, correlation_id),
            )
        return cursor.rowcount == 1

    def upsert_detected_followup(
        self, *, followup_id: str, reason: str, status: str, subject_key: str,
        next_check_at: str | None, expires_at: str, timezone: str,
        confidence_band: str, assumptions: dict[str, Any], now: str,
    ) -> dict[str, Any]:
        goal_id = "detected:local-session"
        with self.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO goals
                (id, profile_key, owner_id, created_at, updated_at, revision, title, status,
                 scope_json, user_intent_ref, parent_goal_id)
                VALUES (?, ?, ?, ?, ?, 1, 'Detected follow-ups', 'active', '{}', NULL, NULL)""",
                (goal_id, *self._scope_values, now, now),
            )
            existing = db.execute(
                """SELECT id, status FROM followups
                   WHERE profile_key=? AND owner_id=? AND subject_key=?
                     AND status NOT IN ('completed','cancelled','expired')""",
                (*self._scope_values, subject_key),
            ).fetchone()
            if existing is not None:
                return {**dict(existing), "created": False}
            db.execute(
                """INSERT INTO followups
                (id, profile_key, owner_id, created_at, updated_at, revision, goal_id, reason, status,
                 subject_key, evidence_revision, next_check_at, deadline_at, expires_at, timezone,
                 confidence_band, assumptions_json, stop_rule_json, last_notified_at, reminder_count)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 1, ?, NULL, ?, ?, ?, ?, '{}', NULL, 0)""",
                (followup_id, *self._scope_values, now, now, goal_id, reason, status, subject_key,
                 next_check_at, expires_at, timezone, confidence_band,
                 canonical_json(self._object(assumptions, "assumptions"))),
            )
        return {"id": followup_id, "status": status, "created": True}

    def claim_due_followups(self, now: str, limit: int) -> list[dict[str, Any]]:
        """Atomically move due ready rows to checking and return immutable work snapshots."""
        limit = max(1, min(int(limit), 100))
        claimed: list[dict[str, Any]] = []
        with self.transaction() as db:
            rows = db.execute(
                """SELECT id, revision, evidence_revision, assumptions_json, next_check_at, reason, task_id
                   FROM followups
                  WHERE profile_key=? AND owner_id=? AND status='ready'
                    AND (next_check_at IS NULL OR next_check_at<=?) AND expires_at>?
                  ORDER BY COALESCE(next_check_at, created_at), id LIMIT ?""",
                (*self._scope_values, now, now, limit),
            ).fetchall()
            for row in rows:
                cursor = db.execute(
                    """UPDATE followups SET status='checking', updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND id=? AND status='ready' AND revision=?""",
                    (now, *self._scope_values, row["id"], row["revision"]),
                )
                if cursor.rowcount == 1:
                    item = dict(row)
                    item["revision"] += 1
                    item["assumptions"] = json.loads(item.pop("assumptions_json"))
                    claimed.append(item)
        return claimed

    def recover_stale_checks(self, stale_before: str, now: str) -> int:
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE followups SET status='ready', updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND status='checking' AND updated_at<?""",
                (now, *self._scope_values, stale_before),
            )
        return cursor.rowcount

    def finish_followup_check(
        self, followup_id: str, *, success: bool, next_check_at: str | None, now: str,
        complete: bool = False,
    ) -> None:
        target = "completed" if complete else ("ready" if success else "failed")
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE followups SET status=?, next_check_at=?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND status='checking'""",
                (target, next_check_at, now, *self._scope_values, followup_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("followup check is no longer owned")

    # -- runs, budgets, audit -------------------------------------------------------------
    def create_run(
        self, *, run_id: str, followup_id: str, plan_id: str, execution_key: str,
        effect_class: str, now: str,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            existing = db.execute(
                """SELECT id, status FROM runs WHERE profile_key=? AND owner_id=? AND execution_key=?""",
                (*self._scope_values, execution_key),
            ).fetchone()
            if existing is not None:
                return {**dict(existing), "created": False}
            db.execute(
                """INSERT INTO runs
                (id, profile_key, owner_id, created_at, updated_at, revision, followup_id, plan_id,
                 consent_id, effect_class, status, execution_key, lease_owner, lease_until,
                 fencing_token, heartbeat_at, attempt, started_at, finished_at, result_json,
                 evidence_refs_json, provider_receipt_json, error_code)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, NULL, ?, 'queued', ?, NULL, NULL, 0, NULL, 0,
                        NULL, NULL, '{}', '[]', NULL, NULL)""",
                (run_id, *self._scope_values, now, now, followup_id, plan_id, effect_class, execution_key),
            )
        return {"id": run_id, "status": "queued", "created": True}

    def start_run(self, run_id: str, *, worker_id: str, lease_until: str, now: str) -> int:
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE runs SET status='running', lease_owner=?, lease_until=?, heartbeat_at=?,
                          started_at=COALESCE(started_at, ?), attempt=attempt+1,
                          fencing_token=fencing_token+1, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND status='queued'""",
                (worker_id, lease_until, now, now, now, *self._scope_values, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("run is not claimable")
            return db.execute(
                """SELECT fencing_token FROM runs WHERE profile_key=? AND owner_id=? AND id=?""",
                (*self._scope_values, run_id),
            ).fetchone()[0]

    def finish_run(
        self, run_id: str, *, fencing_token: int, status: str, result: dict[str, Any],
        error_code: str | None, now: str,
    ) -> None:
        if status not in {"succeeded", "failed", "unknown", "cancelled"}:
            raise ValueError("invalid terminal run status")
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE runs SET status=?, result_json=?, error_code=?, finished_at=?, updated_at=?,
                          lease_owner=NULL, lease_until=NULL, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND status='running' AND fencing_token=?""",
                (status, canonical_json(self._object(result, "result")), error_code, now, now,
                 *self._scope_values, run_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("stale run fencing token")

    def reserve_budget(
        self, *, bucket_start: str, resource_key: str, units: int, limit: int, now: str,
        bucket: str = "checks", unit: str = "check",
    ) -> bool:
        if units < 1 or limit < 1:
            raise ValueError("budget units and limit must be positive")
        budget_id = f"{bucket}:{bucket_start}:{resource_key}"
        with self.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO budgets
                (id, profile_key, owner_id, created_at, updated_at, revision, bucket_type,
                 bucket_start, resource_key, reserved_units, consumed_units, unit)
                VALUES (?, ?, ?, ?, ?, 1, 'day', ?, ?, 0, 0, ?)""",
                (budget_id, *self._scope_values, now, now, bucket_start, f"{bucket}:{resource_key}", unit),
            )
            cursor = db.execute(
                """UPDATE budgets SET reserved_units=reserved_units+?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=?
                     AND reserved_units+consumed_units+?<=?""",
                (units, now, *self._scope_values, budget_id, units, limit),
            )
        return cursor.rowcount == 1

    def settle_budget(
        self, *, bucket_start: str, resource_key: str, units: int, consumed: bool, now: str,
        bucket: str = "checks",
    ) -> None:
        budget_id = f"{bucket}:{bucket_start}:{resource_key}"
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE budgets SET reserved_units=reserved_units-?,
                          consumed_units=consumed_units+?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND reserved_units>=?""",
                (units, units if consumed else 0, now, *self._scope_values, budget_id, units),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("budget reservation is missing")

    def budget_usage(self, bucket_start: str, bucket: str) -> dict[str, dict[str, int]]:
        rows = self.db.execute(
            """SELECT resource_key, reserved_units, consumed_units FROM budgets
               WHERE profile_key=? AND owner_id=? AND bucket_start=? AND resource_key LIKE ?""",
            (*self._scope_values, bucket_start, f"{bucket}:%"),
        ).fetchall()
        return {
            row["resource_key"].split(":", 1)[1]: {"reserved": row["reserved_units"], "consumed": row["consumed_units"]}
            for row in rows
        }

    def add_audit(
        self, *, audit_id: str, event_type: str, subject_type: str, subject_id: str | None,
        correlation_id: str | None, actor_kind: str, decision_code: str,
        metadata: dict[str, Any], now: str,
    ) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO audit
                (id, profile_key, owner_id, created_at, updated_at, revision, event_type,
                 subject_type, subject_id, correlation_id, actor_kind, decision_code,
                 metadata_redacted_json)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
                (audit_id, *self._scope_values, now, now, event_type, subject_type, subject_id,
                 correlation_id, actor_kind, decision_code, canonical_json(self._object(metadata, "metadata"))),
            )

    def list_audit(self, limit: int = 50, subject_id: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        if subject_id:
            rows = self.db.execute(
                """SELECT * FROM audit WHERE profile_key=? AND owner_id=? AND subject_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (*self._scope_values, subject_id, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                """SELECT * FROM audit WHERE profile_key=? AND owner_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (*self._scope_values, limit),
            ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def inspect_object(self, object_id: str) -> dict[str, Any]:
        for table in (
            "sources", "goals", "followups", "plans", "runs", "destinations", "outbox", "proposals",
            "signals", "tasks", "jobs",
        ):
            row = self.db.execute(
                f'SELECT * FROM "{table}" WHERE profile_key=? AND owner_id=? AND id=?',
                (*self._scope_values, object_id),
            ).fetchone()
            if row is not None:
                result = self._decode(row) or {}
                result["object_type"] = table[:-1] if table.endswith("s") else table
                return result
        raise KeyError(object_id)

    # -- destinations and outbox ----------------------------------------------------------
    def verify_destination(
        self, *, destination_id: str, platform: str, gateway_connection_ref: str,
        address: dict[str, Any], owner_platform_id: str, kind: str,
        capabilities: dict[str, Any], now: str,
    ) -> None:
        if kind not in {"private", "group"}:
            raise ValueError("destination kind must be private or group")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO destinations
                (id, profile_key, owner_id, created_at, updated_at, revision, platform,
                 gateway_connection_ref, address_json, owner_platform_id, kind,
                 capabilities_json, verified_at, enabled)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(profile_key, owner_id, id) DO UPDATE SET
                  updated_at=excluded.updated_at, revision=destinations.revision+1,
                  platform=excluded.platform, gateway_connection_ref=excluded.gateway_connection_ref,
                  address_json=excluded.address_json, owner_platform_id=excluded.owner_platform_id,
                  kind=excluded.kind, capabilities_json=excluded.capabilities_json,
                  verified_at=excluded.verified_at, enabled=1""",
                (destination_id, *self._scope_values, now, now, platform, gateway_connection_ref,
                 canonical_json(self._object(address, "address")), owner_platform_id, kind,
                 canonical_json(self._object(capabilities, "capabilities")), now),
            )

    def destination(self, destination_id: str) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT * FROM destinations
               WHERE profile_key=? AND owner_id=? AND id=? AND enabled=1""",
            (*self._scope_values, destination_id),
        ).fetchone()
        if row is None:
            raise KeyError(destination_id)
        result = dict(row)
        result["address"] = json.loads(result.pop("address_json"))
        result["capabilities"] = json.loads(result.pop("capabilities_json"))
        return result

    def enqueue_notification(
        self, *, outbox_id: str, destination_id: str, followup_id: str | None,
        proposal_id: str | None, notification_key: str, payload: dict[str, Any], now: str,
        task_id: str | None = None, kind: str = "notice",
    ) -> dict[str, Any]:
        with self.transaction() as db:
            existing = db.execute(
                """SELECT id, status FROM outbox
                   WHERE profile_key=? AND owner_id=? AND notification_key=?""",
                (*self._scope_values, notification_key),
            ).fetchone()
            if existing is not None:
                return {**dict(existing), "created": False}
            db.execute(
                """INSERT INTO outbox
                (id, profile_key, owner_id, created_at, updated_at, revision, destination_id,
                 followup_id, proposal_id, notification_key, payload_json, status, attempt,
                 next_attempt_at, lease_until, fencing_token, platform_message_id, last_error, task_id, kind)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, 0, NULL, NULL, ?, ?)""",
                (outbox_id, *self._scope_values, now, now, destination_id, followup_id,
                 proposal_id, notification_key, canonical_json(self._object(payload, "payload")), now,
                 task_id, kind),
            )
        return {"id": outbox_id, "status": "pending", "created": True}

    def record_sent(
        self, *, outbox_id: str, destination_id: str, notification_key: str, payload: dict[str, Any],
        platform_message_id: str, now: str, task_id: str | None = None, kind: str = "detail",
    ) -> None:
        """An outbox row for a message already delivered (e.g. an answer sent from a button callback),
        so replies to it resolve like replies to any notice."""
        payload = self._object(payload, "payload")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO outbox
                (id, profile_key, owner_id, created_at, updated_at, revision, destination_id, followup_id, proposal_id,
                 notification_key, payload_json, status, attempt, next_attempt_at, lease_until, fencing_token,
                 platform_message_id, last_error, task_id, kind)
                VALUES (?, ?, ?, ?, ?, 1, ?, NULL, NULL, ?, ?, 'sent', 1, ?, NULL, 1, ?, NULL, ?, ?)""",
                (outbox_id, *self._scope_values, now, now, destination_id, notification_key, canonical_json(payload),
                 now, platform_message_id, task_id, kind),
            )

    def claim_outbox(self, now: str, lease_until: str, limit: int = 10) -> list[dict[str, Any]]:
        claimed: list[dict[str, Any]] = []
        with self.transaction() as db:
            db.execute(
                """UPDATE outbox SET status='pending', updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND status='failed' AND next_attempt_at<=?""",
                (now, *self._scope_values, now),
            )
            rows = db.execute(
                """SELECT o.id, o.revision, o.destination_id, o.payload_json, o.attempt, o.task_id, o.kind,
                          d.platform, d.address_json, d.kind AS destination_kind, d.owner_platform_id
                   FROM outbox o JOIN destinations d
                     ON d.profile_key=o.profile_key AND d.owner_id=o.owner_id AND d.id=o.destination_id
                  WHERE o.profile_key=? AND o.owner_id=? AND o.status='pending'
                    AND o.next_attempt_at<=? AND d.enabled=1
                  ORDER BY o.next_attempt_at, o.id LIMIT ?""",
                (*self._scope_values, now, max(1, min(int(limit), 100))),
            ).fetchall()
            for row in rows:
                cursor = db.execute(
                    """UPDATE outbox SET status='sending', lease_until=?, fencing_token=fencing_token+1,
                              attempt=attempt+1, updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND id=?
                         AND status='pending' AND revision=?""",
                    (lease_until, now, *self._scope_values, row["id"], row["revision"]),
                )
                if cursor.rowcount == 1:
                    item = dict(row)
                    item["payload"] = json.loads(item.pop("payload_json"))
                    item["address"] = json.loads(item.pop("address_json"))
                    item["fencing_token"] = db.execute(
                        """SELECT fencing_token FROM outbox
                           WHERE profile_key=? AND owner_id=? AND id=?""",
                        (*self._scope_values, row["id"]),
                    ).fetchone()[0]
                    claimed.append(item)
        return claimed

    def cancel_outbox(self, outbox_id: str, *, fencing_token: int, error: str, now: str) -> None:
        """A claimed message that must never go out (its decisions were withdrawn before delivery)."""
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE outbox SET status='cancelled', last_error=?, lease_until=NULL, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND status='sending' AND fencing_token=?""",
                (error, now, *self._scope_values, outbox_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("stale outbox fencing token")

    def finish_outbox(
        self, outbox_id: str, *, fencing_token: int, success: bool,
        platform_message_id: str | None, error: str | None, next_attempt_at: str, now: str,
    ) -> None:
        status = "sent" if success else "failed"
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE outbox SET status=?, platform_message_id=?, last_error=?,
                          next_attempt_at=?, lease_until=NULL, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=?
                     AND status='sending' AND fencing_token=?""",
                (status, platform_message_id, error, next_attempt_at, now,
                 *self._scope_values, outbox_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("stale outbox fencing token")

    def notification_count_since(self, since: str, *, include_exempt: bool = False) -> int:
        """Sent notices since ``since``; by default without those exempt from the daily cap (answers to approvals)."""
        exempt = "" if include_exempt else " AND COALESCE(json_extract(payload_json, '$.exempt'), 0) = 0"
        return self.db.execute(
            f"""SELECT COUNT(*) FROM outbox
               WHERE profile_key=? AND owner_id=? AND status='sent' AND updated_at>=?{exempt}""",
            (*self._scope_values, since),
        ).fetchone()[0]

    def outbox_by_platform_message(self, platform_message_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            """SELECT * FROM outbox WHERE profile_key=? AND owner_id=? AND platform_message_id=?
               ORDER BY updated_at DESC LIMIT 1""",
            (*self._scope_values, str(platform_message_id)),
        ).fetchone()
        return self._decode(row)

    def get_outbox(self, outbox_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM outbox WHERE profile_key=? AND owner_id=? AND id=?",
            (*self._scope_values, outbox_id),
        ).fetchone()
        if row is None:
            raise KeyError(outbox_id)
        return self._decode(row) or {}

    def list_outbox(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT * FROM outbox WHERE profile_key=? AND owner_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (*self._scope_values, max(1, min(int(limit), 200))),
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    # -- proposals and consent ------------------------------------------------------------
    def create_proposal(
        self, *, proposal_id: str, followup_id: str, plan_id: str, destination_id: str,
        expires_at: str, priority: str, reason_text: str, suppression_key: str, now: str,
    ) -> None:
        """Legacy consent-bound proposal tied to a plan (mutation path)."""
        self.destination(destination_id)
        with self.transaction() as db:
            db.execute(
                """INSERT INTO proposals
                (id, profile_key, owner_id, created_at, updated_at, revision, followup_id,
                 plan_id, destination_id, status, expires_at, priority, reason_text, suppression_key,
                 action_class, fingerprint)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 'pending', ?, ?, ?, ?, 'external', ?)""",
                (proposal_id, *self._scope_values, now, now, followup_id, plan_id,
                 destination_id, expires_at, priority, reason_text, suppression_key, suppression_key),
            )

    def create_task_proposal(
        self, *, proposal_id: str, task_id: str | None, title: str, reason_text: str,
        action_class: str, operation_id: str | None, params: dict[str, Any], options: list[dict[str, Any]],
        urgency: str, expires_at: str, fingerprint: str, now: str, batch_key: str | None = None,
        destination_id: str | None = None, priority: str = "normal", job_id: str | None = None,
        status: str = "pending",
    ) -> dict[str, Any]:
        """User-facing suggestion. An open or in-flight proposal with the same fingerprint is reused, never duplicated."""
        if action_class not in {"read", "local", "external"}:
            raise ValueError("invalid action class")
        if urgency not in {"low", "normal", "high"}:
            raise ValueError("invalid urgency")
        params = self._object(params, "params")
        options = self._array(options, "options")
        with self.transaction() as db:
            if fingerprint:
                existing = db.execute(
                    """SELECT id, status FROM proposals WHERE profile_key=? AND owner_id=? AND fingerprint=?
                       AND status IN ('draft','pending','sent','snoozed','waiting','accepted','executing')""",
                    (*self._scope_values, fingerprint),
                ).fetchone()
                if existing is not None:
                    return {**dict(existing), "created": False}
                for terminal in db.execute(
                    """SELECT id, status, response_json FROM proposals WHERE profile_key=? AND owner_id=? AND fingerprint=?
                       AND status IN ('declined','cancelled')
                       ORDER BY updated_at DESC LIMIT 5""",
                    (*self._scope_values, fingerprint),
                ).fetchall():
                    try:
                        response = json.loads(terminal["response_json"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        response = {}
                    if isinstance(response, dict) and (response.get("ack") or response.get("auto")):
                        continue  # acknowledged news or an automatic withdrawal is not the owner's refusal
                    return {"id": terminal["id"], "status": terminal["status"], "created": False, "suppressed": True}
            db.execute(
                """INSERT INTO proposals
                (id, profile_key, owner_id, created_at, updated_at, revision, followup_id, plan_id,
                 destination_id, task_id, pattern_id, job_id, status, expires_at, priority, reason_text,
                 suppression_key, action_class, operation_id, params_json, fingerprint, title,
                 options_json, urgency, batch_key)
                VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (proposal_id, *self._scope_values, now, now, destination_id, task_id, job_id, status,
                 expires_at, priority, reason_text, fingerprint or proposal_id, action_class, operation_id,
                 canonical_json(params), fingerprint, title, canonical_json(options), urgency, batch_key),
            )
        return {"id": proposal_id, "status": status, "created": True}

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM proposals WHERE profile_key=? AND owner_id=? AND id=?",
            (*self._scope_values, proposal_id),
        ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        return self._decode(row) or {}

    def list_proposals(
        self, statuses: tuple[str, ...] | None = None, limit: int = 50, task_id: str | None = None,
        outbox_id: str | None = None, newest: bool = False,
    ) -> list[dict[str, Any]]:
        """Oldest first by default; ``newest=True`` returns the most recent rows first (the last N, not the first N)."""
        clauses = ["profile_key=?", "owner_id=?"]
        params: list[Any] = list(self._scope_values)
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            params.extend(statuses)
        if task_id is not None:
            clauses.append("task_id=?")
            params.append(task_id)
        if outbox_id is not None:
            clauses.append("outbox_id=?")
            params.append(outbox_id)
        params.append(max(1, min(int(limit), 200)))
        order = "created_at DESC, id DESC" if newest else "created_at, id"
        rows = self.db.execute(
            f"SELECT * FROM proposals WHERE {' AND '.join(clauses)} ORDER BY {order} LIMIT ?", params
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def mark_proposals_sent(self, proposal_ids: list[str], outbox_id: str, now: str) -> int:
        count = 0
        with self.transaction() as db:
            for proposal_id in proposal_ids:
                cursor = db.execute(
                    """UPDATE proposals SET status='sent', outbox_id=?, updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND id=? AND status IN ('pending','snoozed')""",
                    (outbox_id, now, *self._scope_values, proposal_id),
                )
                count += cursor.rowcount
        return count

    def respond_proposal(
        self, proposal_id: str, *, decision: str, response: dict[str, Any], now: str,
        snoozed_until: str | None = None,
    ) -> dict[str, Any]:
        """Record the owner's answer. ``decision`` is accepted | declined | snoozed."""
        target = {"accepted": "accepted", "declined": "declined", "snoozed": "snoozed"}.get(decision)
        if target is None:
            raise ValueError("invalid proposal decision")
        response = self._object(response, "response")
        with self.transaction() as db:
            row = db.execute(
                "SELECT status FROM proposals WHERE profile_key=? AND owner_id=? AND id=?",
                (*self._scope_values, proposal_id),
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            validate_transition("proposal", row["status"], target)
            db.execute(
                """UPDATE proposals SET status=?, response_json=?, responded_at=?, snoozed_until=?,
                          updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=?""",
                (target, canonical_json(response), now, snoozed_until, now, *self._scope_values, proposal_id),
            )
        return {"id": proposal_id, "status": target}

    def set_proposal_status(self, proposal_id: str, status: str, now: str) -> None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT status FROM proposals WHERE profile_key=? AND owner_id=? AND id=?",
                (*self._scope_values, proposal_id),
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            validate_transition("proposal", row["status"], status)
            db.execute(
                """UPDATE proposals SET status=?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=?""",
                (status, now, *self._scope_values, proposal_id),
            )

    def update_proposal_params(self, proposal_id: str, *, now: str, **changes: Any) -> dict[str, Any]:
        """Merge ``changes`` into ``params_json``; a None value removes the key."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT params_json FROM proposals WHERE profile_key=? AND owner_id=? AND id=?",
                (*self._scope_values, proposal_id),
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            try:
                params = json.loads(row["params_json"] or "{}")
            except json.JSONDecodeError:
                params = {}
            if not isinstance(params, dict):
                params = {}
            for key, value in changes.items():
                if value is None:
                    params.pop(key, None)
                else:
                    params[key] = value
            db.execute(
                """UPDATE proposals SET params_json=?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=?""",
                (canonical_json(params), now, *self._scope_values, proposal_id),
            )
        return self.get_proposal(proposal_id)

    def update_proposal(
        self, proposal_id: str, *, now: str, title: str | None = None, reason_text: str | None = None,
        params: dict[str, Any] | None = None, options: list[dict[str, Any]] | None = None, urgency: str | None = None,
        expires_at: str | None = None, fingerprint: str | None = None, batch_key: str | None = None,
    ) -> dict[str, Any]:
        """Rewrite an open proposal in place (a re-plan restating the same step): the row, its id and the number the
        owner saw stay; ``params`` replaces the whole object. The fingerprint only changes when no other live row
        holds the new one."""
        if urgency is not None and urgency not in {"low", "normal", "high"}:
            raise ValueError("invalid urgency")
        with self.transaction() as db:
            row = db.execute(
                "SELECT status, fingerprint FROM proposals WHERE profile_key=? AND owner_id=? AND id=?",
                (*self._scope_values, proposal_id),
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["status"] not in ("pending", "sent", "snoozed"):
                raise ValueError(f"proposal {proposal_id} is not open")
            sets: list[str] = ["updated_at=?", "revision=revision+1"]
            values: list[Any] = [now]
            for column, value in (("title", title), ("reason_text", reason_text), ("urgency", urgency), ("expires_at", expires_at), ("batch_key", batch_key)):
                if value is not None:
                    sets.append(f"{column}=?")
                    values.append(value)
            if params is not None:
                sets.append("params_json=?")
                values.append(canonical_json(self._object(params, "params")))
            if options is not None:
                sets.append("options_json=?")
                values.append(canonical_json(self._array(options, "options")))
            if fingerprint and fingerprint != row["fingerprint"]:
                clash = db.execute(
                    """SELECT id FROM proposals WHERE profile_key=? AND owner_id=? AND fingerprint=? AND id<>?
                       AND status IN ('draft','pending','sent','snoozed','waiting','accepted','executing')""",
                    (*self._scope_values, fingerprint, proposal_id),
                ).fetchone()
                if clash is None:
                    sets.append("fingerprint=?")
                    values.append(fingerprint)
            db.execute(
                f"UPDATE proposals SET {', '.join(sets)} WHERE profile_key=? AND owner_id=? AND id=?",
                (*values, *self._scope_values, proposal_id),
            )
        return self.get_proposal(proposal_id)

    def supersede_proposals(self, proposal_ids: list[str], *, now: str, by: str = "replan") -> int:
        """Withdraw open proposals a newer plan no longer restates: ``expired`` with ``params.superseded_by``.
        In-flight and settled rows are left alone. Returns how many were withdrawn."""
        count = 0
        with self.transaction() as db:
            for proposal_id in proposal_ids:
                row = db.execute(
                    "SELECT status, params_json FROM proposals WHERE profile_key=? AND owner_id=? AND id=?",
                    (*self._scope_values, proposal_id),
                ).fetchone()
                if row is None or row["status"] not in ("pending", "sent", "snoozed"):
                    continue
                try:
                    params = json.loads(row["params_json"] or "{}")
                except json.JSONDecodeError:
                    params = {}
                if not isinstance(params, dict):
                    params = {}
                params.update({"superseded_by": by or "replan", "superseded_at": now})
                db.execute(
                    """UPDATE proposals SET status='expired', params_json=?, updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND id=?""",
                    (canonical_json(params), now, *self._scope_values, proposal_id),
                )
                count += 1
        return count

    def expire_proposals(self, now: str) -> int:
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE proposals SET status='expired', updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND status IN ('draft','pending','sent','snoozed')
                     AND expires_at<=?""",
                (now, *self._scope_values, now),
            )
        return cursor.rowcount

    def decay_proposals(self, now: str, *, unanswered_before: str, news_before: str) -> dict[str, list[str]]:
        """What the owner never answered and what stopped being news.

        A decision sent before ``unanswered_before`` and still unanswered expires with ``expired_reason`` unanswered
        (its task is returned so the caller can park it); a piece of news that was never delivered by ``news_before``,
        or stayed sent, expires as stale. Nothing is re-asked from here."""
        unanswered: list[str] = []
        stale: list[str] = []
        task_ids: list[str] = []
        with self.transaction() as db:
            rows = db.execute(
                """SELECT id, task_id, operation_id, params_json, status, created_at, updated_at FROM proposals
                   WHERE profile_key=? AND owner_id=? AND status IN ('sent','pending')""",
                self._scope_values,
            ).fetchall()
            for row in rows:
                try:
                    params = json.loads(row["params_json"] or "{}")
                except json.JSONDecodeError:
                    params = {}
                if not isinstance(params, dict):
                    params = {}
                news = row["operation_id"] == "inform" and not params.get("ask")
                if news:
                    if row["status"] == "pending" and (str(row["created_at"] or "") > news_before or str(params.get("deliver_after") or "") > now):
                        continue
                    if row["status"] == "sent" and str(row["updated_at"] or "") > news_before:
                        continue
                    reason, bucket = "stale_news", stale
                elif row["status"] == "sent" and str(row["updated_at"] or "") <= unanswered_before:
                    reason, bucket = "unanswered", unanswered
                    if row["task_id"] and row["task_id"] not in task_ids:
                        task_ids.append(row["task_id"])
                else:
                    continue
                params["expired_reason"] = reason
                db.execute(
                    """UPDATE proposals SET status='expired', params_json=?, updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND id=?""",
                    (canonical_json(params), now, *self._scope_values, row["id"]),
                )
                bucket.append(row["id"])
        return {"unanswered": unanswered, "stale_news": stale, "task_ids": task_ids}

    def park_task(self, task_id: str, *, until: str, now: str, reason: str = "unanswered") -> bool:
        """Put a task to sleep until ``until`` without closing it: the owner did not answer, so nothing is asked
        again until then (or until a new signal reopens it). Its review timers are cancelled."""
        task = self.get_task(task_id)
        if task.get("status") in ("done", "abandoned", "expired"):
            return False
        self.update_task(task_id, now=now, bump_version=False, snoozed_until=until, next_review_at=None)
        if task.get("status") != "snoozed":
            self.set_task_status(task_id, "snoozed", now=now, reason=reason)
        self.cancel_task_followups(task_id, now)
        return True

    def wake_snoozed_proposals(self, now: str) -> int:
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE proposals SET status='pending', snoozed_until=NULL, outbox_id=NULL,
                          updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND status='snoozed'
                     AND snoozed_until IS NOT NULL AND snoozed_until<=?""",
                (now, *self._scope_values, now),
            )
        return cursor.rowcount

    def record_consent(
        self, *, consent_id: str, proposal_id: str, plan_hash: str, decision: str,
        actor_ref: str, verified_transport: str, destination_id: str, expires_at: str,
        token_hash: str, now: str,
    ) -> None:
        if decision not in {"approved", "rejected"}:
            raise ValueError("invalid consent decision")
        with self.transaction() as db:
            proposal = db.execute(
                """SELECT p.status, p.expires_at, p.plan_id, p.destination_id, pl.plan_hash
                   FROM proposals p JOIN plans pl
                     ON pl.profile_key=p.profile_key AND pl.owner_id=p.owner_id AND pl.id=p.plan_id
                  WHERE p.profile_key=? AND p.owner_id=? AND p.id=?""",
                (*self._scope_values, proposal_id),
            ).fetchone()
            if proposal is None or proposal["status"] not in {"pending", "sent"}:
                raise RuntimeError("proposal is not pending")
            if proposal["expires_at"] <= now or expires_at <= now:
                raise RuntimeError("consent is expired")
            if proposal["destination_id"] != destination_id or proposal["plan_hash"] != plan_hash:
                raise RuntimeError("consent context mismatch")
            db.execute(
                """INSERT INTO consents
                (id, profile_key, owner_id, created_at, updated_at, revision, proposal_id,
                 plan_hash, decision, actor_ref, verified_transport, destination_id,
                 expires_at, token_hash, consumed_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
                (consent_id, *self._scope_values, now, now, proposal_id, plan_hash, decision,
                 actor_ref, verified_transport, destination_id, expires_at, token_hash),
            )
            target = "accepted" if decision == "approved" else "declined"
            db.execute(
                """UPDATE proposals SET status=?, responded_at=?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND status IN ('pending','sent')""",
                (target, now, now, *self._scope_values, proposal_id),
            )

    def record_native_consent(
        self, *, consent_id: str, proposal_id: str, plan_hash: str, actor_ref: str, verified_transport: str,
        destination_id: str, expires_at: str, token_hash: str, now: str,
    ) -> None:
        """Single-use consent for a task proposal's native operation (bound to the operation + parameters hash)."""
        with self.transaction() as db:
            proposal = db.execute(
                "SELECT status, expires_at FROM proposals WHERE profile_key=? AND owner_id=? AND id=?",
                (*self._scope_values, proposal_id),
            ).fetchone()
            if proposal is None or proposal["status"] not in {"pending", "sent", "snoozed", "accepted", "waiting"}:
                raise RuntimeError("proposal is not open")
            if expires_at <= now:
                raise RuntimeError("consent is expired")
            db.execute(
                """INSERT INTO consents
                (id, profile_key, owner_id, created_at, updated_at, revision, proposal_id,
                 plan_hash, decision, actor_ref, verified_transport, destination_id,
                 expires_at, token_hash, consumed_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'approved', ?, ?, ?, ?, ?, NULL, NULL)""",
                (consent_id, *self._scope_values, now, now, proposal_id, plan_hash, actor_ref, verified_transport,
                 destination_id, expires_at, token_hash),
            )

    def consume_consent(
        self, consent_id: str, *, plan_hash: str, actor_ref: str,
        destination_id: str, verified_transport: str, now: str,
    ) -> None:
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE consents SET consumed_at=?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND decision='approved'
                     AND plan_hash=? AND actor_ref=? AND destination_id=? AND verified_transport=?
                     AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at>?""",
                (now, now, *self._scope_values, consent_id, plan_hash, actor_ref,
                 destination_id, verified_transport, now),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("consent is invalid, expired, mismatched, or already consumed")

    # -- signals ----------------------------------------------------------------------------
    def add_signal(
        self, *, signal_id: str, source_id: str, kind: str, occurred_at: str, external_ref: str,
        group_key: str, title: str, snippet: str, meta: dict[str, Any], fingerprint: str, now: str,
        next_attempt_at: str | None = None, raw_content: str | None = None,
        raw_expires_at: str | None = None, content_ref: str | None = None,
        expires_at: str | None = None, triage_status: str = "pending", triage_reason: str | None = None,
    ) -> bool:
        meta = self._object(meta, "meta")
        with self.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO signals
                (id, profile_key, owner_id, created_at, updated_at, revision, source_id, kind,
                 occurred_at, external_ref, group_key, title, snippet, meta_json, extract_json,
                 content_ref, raw_content, raw_expires_at, fingerprint, triage_status, triage_reason,
                 attempts, next_attempt_at, lease_until, fencing_token, processed_at, expires_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 0, ?, NULL, 0, NULL, ?)""",
                (signal_id, *self._scope_values, now, now, source_id, kind, occurred_at, external_ref,
                 group_key, title, snippet, canonical_json(meta), content_ref, raw_content, raw_expires_at,
                 fingerprint, triage_status, triage_reason, next_attempt_at or now, expires_at),
            )
        return cursor.rowcount == 1

    def signal_exists(self, source_id: str, external_ref: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM signals WHERE profile_key=? AND owner_id=? AND source_id=? AND external_ref=?",
            (*self._scope_values, source_id, external_ref),
        ).fetchone()
        return row is not None

    def get_signal(self, signal_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM signals WHERE profile_key=? AND owner_id=? AND id=?",
            (*self._scope_values, signal_id),
        ).fetchone()
        if row is None:
            raise KeyError(signal_id)
        return self._decode(row) or {}

    def list_signals(
        self, limit: int = 50, status: str | None = None, kind: str | None = None,
        group_key: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["profile_key=?", "owner_id=?"]
        params: list[Any] = list(self._scope_values)
        if status:
            clauses.append("triage_status=?")
            params.append(status)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if group_key:
            clauses.append("group_key=?")
            params.append(group_key)
        params.append(max(1, min(int(limit), 500)))
        rows = self.db.execute(
            f"SELECT * FROM signals WHERE {' AND '.join(clauses)} ORDER BY occurred_at DESC, id LIMIT ?", params
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def claim_signal_bundles(self, now: str, lease_until: str, max_bundles: int) -> list[dict[str, Any]]:
        """Claim pending signals grouped by ``group_key``; one bundle per group, oldest first.

        A group is due when any of its signals is; the whole group then travels together, so a chat whose
        turns were deferred at different moments is still judged as one conversation.
        """
        bundles: list[dict[str, Any]] = []
        with self.transaction() as db:
            groups = db.execute(
                """SELECT group_key, MIN(next_attempt_at) AS due FROM signals
                   WHERE profile_key=? AND owner_id=? AND triage_status='pending'
                     AND next_attempt_at<=? AND (lease_until IS NULL OR lease_until<?)
                   GROUP BY group_key ORDER BY due, group_key LIMIT ?""",
                (*self._scope_values, now, now, max(1, min(int(max_bundles), 50))),
            ).fetchall()
            for group in groups:
                key = group["group_key"]
                db.execute(
                    """UPDATE signals SET lease_until=?, fencing_token=fencing_token+1, attempts=attempts+1,
                              updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND group_key=? AND triage_status='pending'
                         AND (lease_until IS NULL OR lease_until<?)""",
                    (lease_until, now, *self._scope_values, key, now),
                )
                rows = db.execute(
                    """SELECT * FROM signals WHERE profile_key=? AND owner_id=? AND group_key=?
                         AND triage_status='pending' AND lease_until=?
                       ORDER BY occurred_at, id""",
                    (*self._scope_values, key, lease_until),
                ).fetchall()
                signals = [self._decode(row) or {} for row in rows]
                if signals:
                    bundles.append({"group_key": key, "signals": signals})
        return bundles

    def finish_signals(
        self, signals: list[dict[str, Any]], *, status: str, reason: str | None, now: str,
        extracts: dict[str, dict[str, Any]] | None = None,
    ) -> int:
        if status not in {"ignored", "extracted", "error", "uncertain"}:
            raise ValueError("invalid signal terminal status")
        extracts = extracts or {}
        count = 0
        with self.transaction() as db:
            for signal in signals:
                extract = extracts.get(signal["id"])
                cursor = db.execute(
                    """UPDATE signals SET triage_status=?, triage_reason=?, processed_at=?, lease_until=NULL,
                              extract_json=COALESCE(?, extract_json), updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND id=? AND fencing_token=?""",
                    (status, reason, now, canonical_json(extract) if extract is not None else None, now,
                     *self._scope_values, signal["id"], signal["fencing_token"]),
                )
                count += cursor.rowcount
        return count

    def defer_signals(self, signals: list[dict[str, Any]], *, next_attempt_at: str, error: str, now: str,
                      counts_attempt: bool = True) -> int:
        """Put the signals back in the queue for later. ``counts_attempt=False`` is for waits that are nobody's
        failure (the conversation is still going, the model or the budget is unavailable): the claim that
        brought the bundle here is not charged, so a long chat is never dropped for merely lasting."""
        attempts = "attempts" if counts_attempt else "MAX(0, attempts-1)"
        count = 0
        with self.transaction() as db:
            for signal in signals:
                cursor = db.execute(
                    f"""UPDATE signals SET next_attempt_at=?, triage_reason=?, lease_until=NULL, attempts={attempts},
                              updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND id=? AND triage_status='pending' AND fencing_token=?""",
                    (next_attempt_at, error[:300], now, *self._scope_values, signal["id"], signal["fencing_token"]),
                )
                count += cursor.rowcount
        return count

    def last_notice_at(self, task_id: str) -> str | None:
        """When the owner last got a message about this task (None when never)."""
        row = self.db.execute(
            "SELECT MAX(created_at) FROM outbox WHERE profile_key=? AND owner_id=? AND task_id=? AND status NOT IN ('failed', 'cancelled', 'expired')",
            (*self._scope_values, task_id),
        ).fetchone()
        return row[0] if row and row[0] else None

    def tasks_for_group(self, group_key: str, *, statuses: tuple[str, ...] | None = None, limit: int = 6) -> list[dict[str, Any]]:
        """Tasks already fed by a signal of the same group (the same mail thread, the same PR): the strongest
        candidates when a new message of that thread arrives."""
        if not group_key:
            return []
        clauses = ["t.profile_key=?", "t.owner_id=?", "s.group_key=?"]
        params: list[Any] = [*self._scope_values, group_key]
        if statuses:
            clauses.append("t.status IN (%s)" % ",".join("?" for _ in statuses))
            params.extend(statuses)
        params.append(max(1, min(int(limit), 50)))
        rows = self.db.execute(
            f"""SELECT t.* FROM tasks t
                JOIN task_signals ts ON ts.task_id=t.id AND ts.profile_key=t.profile_key AND ts.owner_id=t.owner_id
                JOIN signals s ON s.id=ts.signal_id AND s.profile_key=t.profile_key AND s.owner_id=t.owner_id
                WHERE {' AND '.join(clauses)} GROUP BY t.id ORDER BY MAX(t.updated_at) DESC LIMIT ?""", params,
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def update_signal(self, signal_id: str, *, now: str, **fields: Any) -> None:
        allowed = {"title", "snippet", "raw_content", "raw_expires_at", "content_ref", "group_key", "meta", "extract"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown signal fields: {sorted(unknown)}")
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key in {"meta", "extract"}:
                sets.append(f"{key}_json=?")
                params.append(canonical_json(self._object(value, key)))
            else:
                sets.append(f"{key}=?")
                params.append(value)
        sets.append("updated_at=?")
        params.append(now)
        sets.append("revision=revision+1")
        with self.transaction() as db:
            db.execute(
                f"UPDATE signals SET {', '.join(sets)} WHERE profile_key=? AND owner_id=? AND id=?",
                (*params, *self._scope_values, signal_id),
            )

    def signals_for_task(self, task_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT s.*, ts.role FROM task_signals ts
               JOIN signals s ON s.profile_key=ts.profile_key AND s.owner_id=ts.owner_id AND s.id=ts.signal_id
              WHERE ts.profile_key=? AND ts.owner_id=? AND ts.task_id=?
              ORDER BY s.occurred_at DESC LIMIT ?""",
            (*self._scope_values, task_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def signals_in_window(self, since: str, until: str, *, kinds: tuple[str, ...] | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        clauses = ["profile_key=?", "owner_id=?", "occurred_at>=?", "occurred_at<=?", "triage_status<>'ignored'"]
        params: list[Any] = [*self._scope_values, since, until]
        if kinds:
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        params.append(max(1, min(int(limit), 20000)))
        rows = self.db.execute(
            f"SELECT id, kind, occurred_at, title, snippet, meta_json FROM signals WHERE {' AND '.join(clauses)} ORDER BY occurred_at LIMIT ?", params
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    # -- patterns ----------------------------------------------------------------------------
    def upsert_pattern(self, *, pattern_id: str, kind: str, fingerprint: str, evidence: dict[str, Any], support: int, confidence: float,
                       now: str, last_seen_at: str | None = None) -> dict[str, Any]:
        """Insert a candidate or refresh an existing pattern's evidence; its status is never reset here."""
        if kind not in {"periodic", "sequence"}:
            raise ValueError("invalid pattern kind")
        evidence = self._object(evidence, "evidence")
        with self.transaction() as db:
            row = db.execute(
                "SELECT id FROM patterns WHERE profile_key=? AND owner_id=? AND fingerprint=?", (*self._scope_values, fingerprint),
            ).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO patterns
                    (id, profile_key, owner_id, created_at, updated_at, revision, kind, fingerprint, name, description, evidence_json,
                     support, confidence, status, task_id, automation_ref, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?, '', '', ?, ?, ?, 'candidate', NULL, NULL, ?)""",
                    (pattern_id, *self._scope_values, now, now, kind, fingerprint, canonical_json(evidence), int(support), float(confidence), last_seen_at),
                )
                pattern_id_final = pattern_id
            else:
                db.execute(
                    """UPDATE patterns SET evidence_json=?, support=?, confidence=?, last_seen_at=COALESCE(?, last_seen_at), updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND id=?""",
                    (canonical_json(evidence), int(support), float(confidence), last_seen_at, now, *self._scope_values, row["id"]),
                )
                pattern_id_final = row["id"]
        return self.get_pattern(pattern_id_final)

    def get_pattern(self, pattern_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM patterns WHERE profile_key=? AND owner_id=? AND id=?", (*self._scope_values, pattern_id)).fetchone()
        if row is None:
            raise KeyError(pattern_id)
        return self._decode(row) or {}

    def pattern_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM patterns WHERE profile_key=? AND owner_id=? AND fingerprint=?", (*self._scope_values, fingerprint)).fetchone()
        return self._decode(row) if row is not None else None

    def list_patterns(self, statuses: tuple[str, ...] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clauses = ["profile_key=?", "owner_id=?"]
        params: list[Any] = list(self._scope_values)
        if statuses:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        params.append(max(1, min(int(limit), 500)))
        rows = self.db.execute(f"SELECT * FROM patterns WHERE {' AND '.join(clauses)} ORDER BY support DESC, updated_at DESC LIMIT ?", params).fetchall()
        return [self._decode(row) or {} for row in rows]

    def set_pattern_status(self, pattern_id: str, status: str, now: str, *, task_id: str | None = None, automation_ref: str | None = None,
                           name: str | None = None, description: str | None = None) -> dict[str, Any]:
        if status not in {"candidate", "proposed", "automated", "dismissed", "expired"}:
            raise ValueError("invalid pattern status")
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE patterns SET status=?, task_id=COALESCE(?, task_id), automation_ref=COALESCE(?, automation_ref),
                          name=COALESCE(?, name), description=COALESCE(?, description), updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=?""",
                (status, task_id, automation_ref, name, description, now, *self._scope_values, pattern_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(pattern_id)
        return self.get_pattern(pattern_id)

    def last_session_turn_at(self, session_id: str) -> str | None:
        row = self.db.execute(
            """SELECT MAX(occurred_at) FROM signals WHERE profile_key=? AND owner_id=? AND kind='session_turn'
               AND json_extract(meta_json, '$.session_id')=?""",
            (*self._scope_values, session_id),
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    # -- card kinds ----------------------------------------------------------------------------
    def upsert_card_kind(self, *, kind_id: str, label: dict[str, str], icon: str, hue: int, layout: str, spec: dict[str, Any], origin: str,
                         support: int, now: str, status: str = "active") -> dict[str, Any]:
        if origin not in {"builtin", "generated"} or status not in {"active", "disabled"}:
            raise ValueError("invalid card kind")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO card_kinds (id, profile_key, owner_id, created_at, updated_at, revision, label_json, icon, hue, layout, spec_json, origin, status, support)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(profile_key, owner_id, id) DO UPDATE SET label_json=excluded.label_json, icon=excluded.icon, hue=excluded.hue,
                     layout=excluded.layout, spec_json=excluded.spec_json, support=excluded.support, updated_at=excluded.updated_at, revision=card_kinds.revision+1""",
                (kind_id, *self._scope_values, now, now, canonical_json(self._object(label, "label")), icon, int(hue), layout,
                 canonical_json(self._object(spec, "spec")), origin, status, int(support)),
            )
        return self.get_card_kind(kind_id)

    def get_card_kind(self, kind_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM card_kinds WHERE profile_key=? AND owner_id=? AND id=?", (*self._scope_values, kind_id)).fetchone()
        if row is None:
            raise KeyError(kind_id)
        return self._decode(row) or {}

    def list_card_kinds(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM card_kinds WHERE profile_key=? AND owner_id=? ORDER BY origin, id", self._scope_values).fetchall()
        values = [self._decode(row) or {} for row in rows]
        return [v for v in values if not active_only or v.get("status") == "active"]

    def set_card_kind_status(self, kind_id: str, status: str, now: str) -> None:
        if status not in {"active", "disabled"}:
            raise ValueError("invalid card kind status")
        with self.transaction() as db:
            cursor = db.execute("UPDATE card_kinds SET status=?, updated_at=?, revision=revision+1 WHERE profile_key=? AND owner_id=? AND id=?",
                                (status, now, *self._scope_values, kind_id))
            if cursor.rowcount != 1:
                raise KeyError(kind_id)

    def pending_signal_count(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM signals WHERE profile_key=? AND owner_id=? AND triage_status='pending'",
            self._scope_values,
        ).fetchone()[0]

    def prune_signals(self, now: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.transaction() as db:
            raw = db.execute(
                """UPDATE signals SET raw_content=NULL, raw_expires_at=NULL, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND raw_content IS NOT NULL
                     AND raw_expires_at IS NOT NULL AND raw_expires_at<=?""",
                (now, *self._scope_values, now),
            )
            counts["raw_content"] = raw.rowcount
            db.execute(
                """DELETE FROM task_signals WHERE profile_key=? AND owner_id=? AND signal_id IN (
                     SELECT s.id FROM signals s
                      WHERE s.profile_key=? AND s.owner_id=? AND s.expires_at IS NOT NULL AND s.expires_at<=?
                        AND NOT EXISTS (
                          SELECT 1 FROM task_signals x JOIN tasks t
                            ON t.profile_key=x.profile_key AND t.owner_id=x.owner_id AND t.id=x.task_id
                           WHERE x.profile_key=s.profile_key AND x.owner_id=s.owner_id AND x.signal_id=s.id
                             AND t.status NOT IN ('done','abandoned','expired')))""",
                (*self._scope_values, *self._scope_values, now),
            )
            expired = db.execute(
                """DELETE FROM signals WHERE profile_key=? AND owner_id=? AND expires_at IS NOT NULL AND expires_at<=?
                     AND NOT EXISTS (
                       SELECT 1 FROM task_signals x WHERE x.profile_key=signals.profile_key
                          AND x.owner_id=signals.owner_id AND x.signal_id=signals.id)""",
                (*self._scope_values, now),
            )
            counts["signals"] = expired.rowcount
        return counts

    # -- tasks ------------------------------------------------------------------------------
    def _refresh_task_fts(self, db: sqlite3.Connection, task_id: str, title: str, summary: str, entities: dict[str, Any]) -> None:
        db.execute("DELETE FROM tasks_fts WHERE task_id=? AND profile_key=? AND owner_id=?", (task_id, *self._scope_values))
        db.execute(
            "INSERT INTO tasks_fts(task_id, profile_key, owner_id, title, summary, entities) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, *self._scope_values, title, summary, _entities_text(entities)),
        )

    def create_task(
        self, *, task_id: str, kind: str, title: str, summary: str, entities: dict[str, Any],
        expectations: list[Any], checklist: list[Any], now: str, next_review_at: str | None = None,
        due_at: str | None = None, confidence: float = 0.5, origin_signal_id: str | None = None,
        status: str = "open",
    ) -> dict[str, Any]:
        entities = self._object(entities, "entities")
        expectations = self._array(expectations, "expectations")
        checklist = self._array(checklist, "checklist")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO tasks
                (id, profile_key, owner_id, created_at, updated_at, revision, kind, title, summary, status,
                 entities_json, expectations_json, checklist_json, next_review_at, due_at, snoozed_until,
                 confidence, origin_signal_id, closed_reason, muted, version)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, 0, 1)""",
                (task_id, *self._scope_values, now, now, kind, title, summary, status, canonical_json(entities),
                 canonical_json(expectations), canonical_json(checklist), next_review_at, due_at,
                 float(confidence), origin_signal_id),
            )
            self._refresh_task_fts(db, task_id, title, summary, entities)
        return {"id": task_id, "status": status, "version": 1}

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM tasks WHERE profile_key=? AND owner_id=? AND id=?",
            (*self._scope_values, task_id),
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._decode(row) or {}

    def list_tasks(
        self, statuses: tuple[str, ...] | None = None, limit: int = 50, include_muted: bool = True,
        kinds: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["profile_key=?", "owner_id=?"]
        params: list[Any] = list(self._scope_values)
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            params.extend(statuses)
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" for _ in kinds))
            params.extend(kinds)
        if not include_muted:
            clauses.append("muted=0")
        params.append(max(1, min(int(limit), 500)))
        rows = self.db.execute(
            f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id LIMIT ?", params
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def tasks_for_session(self, session_id: str, *, statuses: tuple[str, ...] | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Tasks born from one chat session (``entities.session_id``)."""
        if not session_id:
            return []
        clauses = ["profile_key=?", "owner_id=?", "json_extract(entities_json, '$.session_id')=?"]
        params: list[Any] = [*self._scope_values, session_id]
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            params.extend(statuses)
        params.append(max(1, min(int(limit), 100)))
        rows = self.db.execute(
            f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id LIMIT ?", params
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def backfill_task_sessions(self, now: str) -> int:
        """Give tasks born from a chat turn before session ids were recorded their ``entities.session_id``."""
        rows = self.db.execute(
            """SELECT t.id AS task_id, json_extract(s.meta_json, '$.session_id') AS session_id
               FROM tasks t JOIN signals s ON s.profile_key=t.profile_key AND s.owner_id=t.owner_id AND s.id=t.origin_signal_id
               WHERE t.profile_key=? AND t.owner_id=? AND s.kind='session_turn'
                 AND json_extract(t.entities_json, '$.session_id') IS NULL""",
            self._scope_values,
        ).fetchall()
        count = 0
        for row in rows:
            if not row["session_id"]:
                continue
            try:
                task = self.get_task(row["task_id"])
                entities = dict(task.get("entities") or {})
                entities["session_id"] = str(row["session_id"])
                self.update_task(task["id"], now=now, bump_version=False, entities=entities)
                count += 1
            except (KeyError, ValueError, RuntimeError):
                continue
        return count

    def search_tasks(
        self, query: str, *, kinds: tuple[str, ...] | None = None, limit: int = 5, open_only: bool = True,
    ) -> list[dict[str, Any]]:
        tokens = []
        for token in _FTS_TOKEN.findall(query or ""):
            lowered = token.lower()
            if lowered not in tokens:
                tokens.append(lowered)
            if len(tokens) >= 16:
                break
        if not tokens:
            return []
        match = " OR ".join('"' + token.replace('"', '') + '"' for token in tokens)
        clauses = ["f.profile_key=?", "f.owner_id=?", "tasks_fts MATCH ?"]
        params: list[Any] = [*self._scope_values, match]
        if open_only:
            clauses.append("t.status NOT IN ('done','abandoned','expired')")
        if kinds:
            clauses.append("t.kind IN (%s)" % ",".join("?" for _ in kinds))
            params.extend(kinds)
        params.append(max(1, min(int(limit), 50)))
        try:
            rows = self.db.execute(
                f"""SELECT t.*, bm25(tasks_fts) AS score FROM tasks_fts f
                    JOIN tasks t ON t.profile_key=f.profile_key AND t.owner_id=f.owner_id AND t.id=f.task_id
                    WHERE {' AND '.join(clauses)} ORDER BY score LIMIT ?""",
                params,
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [self._decode(row) or {} for row in rows]

    def update_task(self, task_id: str, *, now: str, bump_version: bool = True, **fields: Any) -> dict[str, Any]:
        allowed = {
            "title", "summary", "entities", "expectations", "checklist", "next_review_at", "due_at",
            "confidence", "kind", "snoozed_until", "muted",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        with self.transaction() as db:
            current = db.execute(
                "SELECT * FROM tasks WHERE profile_key=? AND owner_id=? AND id=?",
                (*self._scope_values, task_id),
            ).fetchone()
            if current is None:
                raise KeyError(task_id)
            sets: list[str] = []
            params: list[Any] = []
            for key, value in fields.items():
                if key == "entities":
                    sets.append("entities_json=?")
                    params.append(canonical_json(self._object(value, key)))
                elif key in {"expectations", "checklist"}:
                    sets.append(f"{key}_json=?")
                    params.append(canonical_json(self._array(value, key)))
                elif key == "muted":
                    sets.append("muted=?")
                    params.append(int(bool(value)))
                else:
                    sets.append(f"{key}=?")
                    params.append(value)
            sets.append("updated_at=?")
            params.append(now)
            sets.append("revision=revision+1")
            if bump_version:
                sets.append("version=version+1")
            db.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE profile_key=? AND owner_id=? AND id=?",
                (*params, *self._scope_values, task_id),
            )
            if {"title", "summary", "entities"} & set(fields):
                title = fields.get("title", current["title"])
                summary = fields.get("summary", current["summary"])
                entities = fields.get("entities", json.loads(current["entities_json"]))
                self._refresh_task_fts(db, task_id, title, summary, entities)
        return self.get_task(task_id)

    def set_task_status(self, task_id: str, status: str, *, now: str, reason: str | None = None) -> dict[str, Any]:
        """Move a task; reaching a terminal state withdraws every open proposal, so a settled task asks nothing more."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT status FROM tasks WHERE profile_key=? AND owner_id=? AND id=?",
                (*self._scope_values, task_id),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] != status:
                validate_transition("task", row["status"], status)
            db.execute(
                """UPDATE tasks SET status=?, closed_reason=?, updated_at=?, revision=revision+1, version=version+1,
                          snoozed_until=CASE WHEN ?='snoozed' THEN snoozed_until ELSE NULL END
                   WHERE profile_key=? AND owner_id=? AND id=?""",
                (status, reason, now, status, *self._scope_values, task_id),
            )
            if status in TASK_TERMINAL:
                db.execute(
                    """UPDATE proposals SET status='expired', updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND task_id=? AND status IN ('draft','pending','snoozed','sent')""",
                    (now, *self._scope_values, task_id),
                )
        return self.get_task(task_id)

    def link_task_signal(self, task_id: str, signal_id: str, role: str, now: str) -> bool:
        with self.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO task_signals
                (id, profile_key, owner_id, created_at, updated_at, revision, task_id, signal_id, role)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (f"{task_id}:{signal_id}:{role}", *self._scope_values, now, now, task_id, signal_id, role),
            )
        return cursor.rowcount == 1

    def tasks_due_review(self, now: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT * FROM tasks WHERE profile_key=? AND owner_id=?
                 AND status NOT IN ('done','abandoned','expired')
                 AND ((next_review_at IS NOT NULL AND next_review_at<=?)
                      OR (status='snoozed' AND snoozed_until IS NOT NULL AND snoozed_until<=?))
               ORDER BY COALESCE(next_review_at, snoozed_until), id LIMIT ?""",
            (*self._scope_values, now, now, max(1, min(int(limit), 100))),
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def open_task_summaries(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT id, kind, title, summary, status, due_at, next_review_at, version FROM tasks
               WHERE profile_key=? AND owner_id=? AND status NOT IN ('done','abandoned','expired')
               ORDER BY updated_at DESC LIMIT ?""",
            (*self._scope_values, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- jobs -------------------------------------------------------------------------------
    def create_job(
        self, *, job_id: str, task_id: str | None, proposal_id: str | None, kind: str, prompt: str,
        action_class: str, now: str, workdir: str | None = None, worktree: bool = False,
        skills: list[str] | None = None, toolsets: list[str] | None = None,
    ) -> dict[str, Any]:
        if action_class not in {"read", "local", "external"}:
            raise ValueError("invalid action class")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO jobs
                (id, profile_key, owner_id, created_at, updated_at, revision, task_id, proposal_id, kind, prompt,
                 workdir, worktree, skills_json, toolsets_json, action_class, status, pid, session_ref,
                 result_summary, result_json, usage_json, lease_until, fencing_token, attempt, started_at,
                 finished_at, error)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL, '{}', '{}',
                        NULL, 0, 0, NULL, NULL, NULL)""",
                (job_id, *self._scope_values, now, now, task_id, proposal_id, kind, prompt, workdir,
                 int(bool(worktree)), canonical_json(self._string_list(skills or [], "skills")),
                 canonical_json(self._string_list(toolsets or [], "toolsets")), action_class),
            )
        return {"id": job_id, "status": "queued"}

    def claim_queued_jobs(self, now: str, lease_until: str, limit: int) -> list[dict[str, Any]]:
        claimed: list[dict[str, Any]] = []
        with self.transaction() as db:
            rows = db.execute(
                """SELECT * FROM jobs WHERE profile_key=? AND owner_id=? AND status='queued'
                   ORDER BY created_at, id LIMIT ?""",
                (*self._scope_values, max(1, min(int(limit), 20))),
            ).fetchall()
            for row in rows:
                cursor = db.execute(
                    """UPDATE jobs SET status='running', lease_until=?, fencing_token=fencing_token+1,
                              attempt=attempt+1, started_at=COALESCE(started_at, ?), updated_at=?, revision=revision+1
                       WHERE profile_key=? AND owner_id=? AND id=? AND status='queued' AND revision=?""",
                    (lease_until, now, now, *self._scope_values, row["id"], row["revision"]),
                )
                if cursor.rowcount == 1:
                    item = self._decode(row) or {}
                    item["fencing_token"] = row["fencing_token"] + 1
                    claimed.append(item)
        return claimed

    def set_job_workdir(self, job_id: str, workdir: str, now: str) -> None:
        """The checkout a job ended up working in (a clone made on its behalf), kept for later steps."""
        with self.transaction() as db:
            db.execute(
                "UPDATE jobs SET workdir=?, worktree=1, updated_at=? WHERE profile_key=? AND owner_id=? AND id=?",
                (workdir, now, *self._scope_values, job_id),
            )

    def set_job_pid(self, job_id: str, pid: int | None, now: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE jobs SET pid=?, updated_at=? WHERE profile_key=? AND owner_id=? AND id=?",
                (pid, now, *self._scope_values, job_id),
            )

    def finish_job(
        self, job_id: str, *, fencing_token: int, status: str, summary: str | None, result: dict[str, Any],
        usage: dict[str, Any], error: str | None, now: str, session_ref: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "timed_out", "cancelled", "unknown"}:
            raise ValueError("invalid terminal job status")
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE jobs SET status=?, result_summary=?, result_json=?, usage_json=?, error=?,
                          session_ref=COALESCE(?, session_ref), finished_at=?, lease_until=NULL, pid=NULL,
                          updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND status='running' AND fencing_token=?""",
                (status, summary, canonical_json(self._object(result, "result")),
                 canonical_json(self._object(usage, "usage")), error, session_ref, now, now,
                 *self._scope_values, job_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("stale job fencing token")

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM jobs WHERE profile_key=? AND owner_id=? AND id=?",
            (*self._scope_values, job_id),
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._decode(row) or {}

    def list_jobs(self, statuses: tuple[str, ...] | None = None, limit: int = 50, task_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["profile_key=?", "owner_id=?"]
        params: list[Any] = list(self._scope_values)
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            params.extend(statuses)
        if task_id is not None:
            clauses.append("task_id=?")
            params.append(task_id)
        params.append(max(1, min(int(limit), 200)))
        rows = self.db.execute(
            f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id LIMIT ?", params
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def recover_stale_jobs(self, now: str) -> int:
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE jobs SET status='unknown', error='lease expired', finished_at=?, lease_until=NULL,
                          updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND status='running' AND lease_until IS NOT NULL AND lease_until<?""",
                (now, now, *self._scope_values, now),
            )
        return cursor.rowcount

    # -- facts and preferences --------------------------------------------------------------
    def upsert_fact(self, *, key: str, value: str, confidence: float, provenance: dict[str, Any], now: str) -> None:
        key = " ".join(str(key).split()).lower()
        if not key or not isinstance(value, str) or not value.strip():
            raise ValueError("fact key and value are required")
        confidence = max(0.0, min(1.0, float(confidence)))
        provenance = self._object(provenance, "provenance")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO facts
                (id, profile_key, owner_id, created_at, updated_at, revision, key, value, confidence, provenance_json)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(profile_key, owner_id, key) DO UPDATE SET
                  updated_at=excluded.updated_at, revision=facts.revision+1,
                  value=CASE WHEN excluded.confidence>=facts.confidence THEN excluded.value ELSE facts.value END,
                  confidence=MAX(facts.confidence, excluded.confidence),
                  provenance_json=CASE WHEN excluded.confidence>=facts.confidence THEN excluded.provenance_json ELSE facts.provenance_json END""",
                (f"fact:{key}", *self._scope_values, now, now, key, value.strip(), confidence, canonical_json(provenance)),
            )

    def record_owner_fact(self, *, key: str, value: str, provenance: dict[str, Any], now: str) -> None:
        """A fact the owner stated themselves: full confidence, origin "owner", never overridden by a guess."""
        self.upsert_fact(key=key, value=value, confidence=1.0, provenance={**provenance, "origin": "owner"}, now=now)

    def list_facts(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT key, value, confidence, provenance_json, updated_at FROM facts
               WHERE profile_key=? AND owner_id=? ORDER BY key LIMIT ?""",
            (*self._scope_values, max(1, min(int(limit), 500))),
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def delete_fact(self, key: str) -> bool:
        with self.transaction() as db:
            cursor = db.execute(
                "DELETE FROM facts WHERE profile_key=? AND owner_id=? AND key=?",
                (*self._scope_values, " ".join(str(key).split()).lower()),
            )
        return cursor.rowcount == 1

    def save_preference(self, data: dict[str, Any], *, expected_version: int, now: str, feedback_ref: str | None = None) -> int:
        from .preferences import validate_preference

        validate_preference(data)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if data["origin"] == "inferred" and data["status"] == "active":
            raise ValueError("inferred preference requires explicit acceptance")
        with self.transaction() as db:
            current = db.execute(
                "SELECT COALESCE(MAX(version), 0) FROM preferences WHERE profile_key=? AND owner_id=? AND id=?",
                (*self._scope_values, data["id"]),
            ).fetchone()[0]
            if current != expected_version:
                raise ValueError("stale preference version")
            version = current + 1
            db.execute(
                """INSERT INTO preferences
                (id, profile_key, owner_id, created_at, updated_at, revision, version, dimension, scope_json,
                 effect, exceptions_json, instruction, origin, status, feedback_ref)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (data["id"], *self._scope_values, now, now, version, data["dimension"],
                 canonical_json(data["scope"]), data["effect"], canonical_json(data["exceptions"]),
                 data["instruction"], data["origin"], data["status"], feedback_ref),
            )
        return version

    def list_preferences(self, active_only: bool = False) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT p.* FROM preferences p
               WHERE p.profile_key=? AND p.owner_id=? AND p.version=(
                 SELECT MAX(q.version) FROM preferences q
                  WHERE q.profile_key=p.profile_key AND q.owner_id=p.owner_id AND q.id=p.id)
               ORDER BY p.id""",
            self._scope_values,
        ).fetchall()
        values = [self._decode(row) or {} for row in rows]
        return [v for v in values if not active_only or v.get("status") == "active"]

    def preference_history(self, preference_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM preferences WHERE profile_key=? AND owner_id=? AND id=? ORDER BY version",
            (*self._scope_values, preference_id),
        ).fetchall()
        return [self._decode(row) or {} for row in rows]

    # -- housekeeping -----------------------------------------------------------------------
    def prune_expired(self, now: str, audit_before: str | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.transaction() as db:
            links = db.execute(
                """DELETE FROM followup_evidence
                   WHERE profile_key=? AND owner_id=? AND evidence_id IN (
                     SELECT id FROM evidence
                      WHERE profile_key=? AND owner_id=? AND expires_at IS NOT NULL AND expires_at<=?
                   )""",
                (*self._scope_values, *self._scope_values, now),
            )
            counts["followup_evidence"] = links.rowcount
            evidence = db.execute(
                """DELETE FROM evidence WHERE profile_key=? AND owner_id=?
                     AND expires_at IS NOT NULL AND expires_at<=?""",
                (*self._scope_values, now),
            )
            counts["evidence"] = evidence.rowcount
            observations = db.execute(
                """DELETE FROM observations AS o
                    WHERE o.profile_key=? AND o.owner_id=?
                      AND o.expires_at IS NOT NULL AND o.expires_at<=?
                      AND NOT EXISTS (
                        SELECT 1 FROM evidence e
                         WHERE e.profile_key=o.profile_key AND e.owner_id=o.owner_id
                           AND e.observation_id=o.id
                      )""",
                (*self._scope_values, now),
            )
            counts["observations"] = observations.rowcount
            if audit_before:
                audit = db.execute(
                    "DELETE FROM audit WHERE profile_key=? AND owner_id=? AND created_at<?",
                    (*self._scope_values, audit_before),
                )
                counts["audit"] = audit.rowcount
        counts.update(self.prune_signals(now))
        return counts

    def close(self) -> None:
        self._closed = True
        with self._conn_lock:
            connections = list(self._connections)
            self._connections.clear()
        for db in connections:
            try:
                db.close()
            except Exception:
                pass
        self._local = threading.local()


def _entities_text(entities: Any) -> str:
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                parts.append(str(key))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif value is not None:
            parts.append(str(value))

    walk(entities)
    return " ".join(parts)[:4000]

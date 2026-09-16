from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .models import PlanIdentity, canonical_json, validate_transition


@dataclass(frozen=True)
class Scope:
    profile_key: str
    owner_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.profile_key, str) or not self.profile_key.strip():
            raise ValueError("profile_key is required")
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise ValueError("owner_id is required")


class Store:
    """SQLite owned by the plugin; every repository operation is scope-bound."""

    migration_dir = Path(__file__).parent / "migrations"

    def __init__(self, path: str | os.PathLike[str], scope: Scope):
        self.path = Path(path)
        self.scope = scope
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @property
    def _scope_values(self) -> tuple[str, str]:
        return self.scope.profile_key, self.scope.owner_id

    def _migrate(self) -> None:
        self.db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        current = self.db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        files = sorted(self.migration_dir.glob("*.sql"))
        for number, migration in enumerate(files, 1):
            if number <= current:
                continue
            script = migration.read_text(encoding="utf-8")
            try:
                self.db.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + script
                    + f"\nINSERT INTO schema_version(version) VALUES ({number});\nCOMMIT;"
                )
            except sqlite3.DatabaseError:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except Exception:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    @staticmethod
    def _object(value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be an object")
        canonical_json(value)
        return value

    @staticmethod
    def _string_list(value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            raise ValueError(f"{field} must be an array of non-empty strings")
        return value

    def status(self) -> dict[str, Any]:
        tables = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'schema_version' ORDER BY name"
        ).fetchall()
        counts = {
            row[0]: self.db.execute(
                f'SELECT COUNT(*) FROM "{row[0]}" WHERE profile_key=? AND owner_id=?', self._scope_values
            ).fetchone()[0]
            for row in tables
        }
        version = self.db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        return {"schema_version": version, "scope": vars(self.scope), "counts": counts}

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
                    source_id,
                    *self._scope_values,
                    now,
                    now,
                    kind,
                    account_ref,
                    canonical_json(resource_scope),
                    int(enabled),
                    permission_revision,
                    canonical_json(cursor),
                    retention_days,
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
                    grant_id,
                    *self._scope_values,
                    now,
                    now,
                    permission,
                    source_id,
                    canonical_json(operation_ids),
                    canonical_json(resource_scope),
                    destination_id,
                    canonical_json(limits),
                    expires_at,
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

    def add_goal(self, goal_id: str, title: str, now: str) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO goals
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
    ) -> None:
        assumptions = self._object(assumptions, "assumptions")
        stop_rule = self._object(stop_rule, "stop_rule")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO followups
                (id, profile_key, owner_id, created_at, updated_at, revision, goal_id, reason, status,
                 subject_key, evidence_revision, next_check_at, deadline_at, expires_at, timezone,
                 confidence_band, assumptions_json, stop_rule_json, last_notified_at, reminder_count)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)""",
                (
                    followup_id,
                    *self._scope_values,
                    now,
                    now,
                    goal_id,
                    reason,
                    status,
                    subject_key,
                    evidence_revision,
                    next_check_at,
                    deadline_at,
                    expires_at,
                    timezone,
                    confidence_band,
                    canonical_json(assumptions),
                    canonical_json(stop_rule),
                ),
            )

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
                    plan_id,
                    *self._scope_values,
                    now,
                    now,
                    followup_id,
                    operation_id,
                    operation_version,
                    canonical_json(parameters),
                    identity.parameters_hash,
                    effect_class,
                    evidence_revision,
                    identity.plan_hash,
                    permission_revision,
                    source_id,
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
                      expires_at, revision, created_at, updated_at
               FROM followups WHERE profile_key=? AND owner_id=? AND id=?""",
            (*self._scope_values, followup_id),
        ).fetchone()
        if row is None:
            raise KeyError(followup_id)
        return dict(row)

    def list_followups(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        rows = self.db.execute(
            """SELECT id, goal_id, reason, status, subject_key, next_check_at, confidence_band
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
        tables = {"followup": "followups", "proposal": "proposals", "run": "runs", "outbox": "outbox"}
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
                (goal_id, *self._scope_values, now, now, f"Monitorizar {host}", canonical_json(resource_scope), source_key),
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
                   WHERE profile_key=? AND owner_id=? AND kind='http_status'
                     AND id LIKE 'configured:%' AND enabled=1""",
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

    def ensure_local_observation_source(self, now: str, retention_days: int) -> str:
        source_id = "local:session-hooks"
        with self.transaction() as db:
            db.execute(
                """INSERT INTO sources
                (id, profile_key, owner_id, created_at, updated_at, revision, kind, account_ref,
                 resource_scope_json, enabled, permission_revision, cursor_json, last_success_at, retention_days)
                VALUES (?, ?, ?, ?, ?, 1, 'local_session', 'hermes-hooks', '{}', 1, 1, '{}', NULL, ?)
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
                VALUES (?, ?, ?, ?, ?, 1, 'Seguimientos detectados', 'active', '{}', NULL, NULL)""",
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
                """SELECT id, revision, evidence_revision, assumptions_json, next_check_at
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

    def finish_followup_check(self, followup_id: str, *, success: bool, next_check_at: str, now: str) -> None:
        target = "ready" if success else "failed"
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE followups SET status=?, next_check_at=?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND status='checking'""",
                (target, next_check_at, now, *self._scope_values, followup_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("followup check is no longer owned")

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
    ) -> bool:
        if units < 1 or limit < 1:
            raise ValueError("budget units and limit must be positive")
        budget_id = f"checks:{bucket_start}:{resource_key}"
        with self.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO budgets
                (id, profile_key, owner_id, created_at, updated_at, revision, bucket_type,
                 bucket_start, resource_key, reserved_units, consumed_units, unit)
                VALUES (?, ?, ?, ?, ?, 1, 'day', ?, ?, 0, 0, 'check')""",
                (budget_id, *self._scope_values, now, now, bucket_start, resource_key),
            )
            cursor = db.execute(
                """UPDATE budgets SET reserved_units=reserved_units+?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=?
                     AND reserved_units+consumed_units+?<=?""",
                (units, now, *self._scope_values, budget_id, units, limit),
            )
        return cursor.rowcount == 1

    def settle_budget(self, *, bucket_start: str, resource_key: str, units: int, consumed: bool, now: str) -> None:
        budget_id = f"checks:{bucket_start}:{resource_key}"
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE budgets SET reserved_units=reserved_units-?,
                          consumed_units=consumed_units+?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND reserved_units>=?""",
                (units, units if consumed else 0, now, *self._scope_values, budget_id, units),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("budget reservation is missing")

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

    def inspect_object(self, object_id: str) -> dict[str, Any]:
        for table in ("sources", "goals", "followups", "plans", "runs", "destinations", "outbox", "proposals"):
            row = self.db.execute(
                f'SELECT * FROM "{table}" WHERE profile_key=? AND owner_id=? AND id=?',
                (*self._scope_values, object_id),
            ).fetchone()
            if row is not None:
                result = dict(row)
                result["object_type"] = table[:-1] if table.endswith("s") else table
                for key in list(result):
                    if key.endswith("_json") and isinstance(result[key], str):
                        try:
                            result[key[:-5]] = json.loads(result.pop(key))
                        except json.JSONDecodeError:
                            pass
                return result
        raise KeyError(object_id)

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
                 next_attempt_at, lease_until, fencing_token, platform_message_id, last_error)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, 0, NULL, NULL)""",
                (outbox_id, *self._scope_values, now, now, destination_id, followup_id,
                 proposal_id, notification_key, canonical_json(self._object(payload, "payload")), now),
            )
        return {"id": outbox_id, "status": "pending", "created": True}

    def claim_outbox(self, now: str, lease_until: str, limit: int = 10) -> list[dict[str, Any]]:
        claimed: list[dict[str, Any]] = []
        with self.transaction() as db:
            db.execute(
                """UPDATE outbox SET status='pending', updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND status='failed' AND next_attempt_at<=?""",
                (now, *self._scope_values, now),
            )
            rows = db.execute(
                """SELECT o.id, o.revision, o.destination_id, o.payload_json, o.attempt,
                          d.platform, d.address_json, d.kind, d.owner_platform_id
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

    def notification_count_since(self, since: str) -> int:
        return self.db.execute(
            """SELECT COUNT(*) FROM outbox
               WHERE profile_key=? AND owner_id=? AND status='sent' AND updated_at>=?""",
            (*self._scope_values, since),
        ).fetchone()[0]

    def create_proposal(
        self, *, proposal_id: str, followup_id: str, plan_id: str, destination_id: str,
        expires_at: str, priority: str, reason_text: str, suppression_key: str, now: str,
    ) -> None:
        self.destination(destination_id)
        with self.transaction() as db:
            db.execute(
                """INSERT INTO proposals
                (id, profile_key, owner_id, created_at, updated_at, revision, followup_id,
                 plan_id, destination_id, status, expires_at, priority, reason_text, suppression_key)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (proposal_id, *self._scope_values, now, now, followup_id, plan_id,
                 destination_id, expires_at, priority, reason_text, suppression_key),
            )

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
            if proposal is None or proposal["status"] != "pending":
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
            target = "accepted" if decision == "approved" else "rejected"
            db.execute(
                """UPDATE proposals SET status=?, updated_at=?, revision=revision+1
                   WHERE profile_key=? AND owner_id=? AND id=? AND status='pending'""",
                (target, now, *self._scope_values, proposal_id),
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

    def prune_expired(self, now: str) -> dict[str, int]:
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
        return counts

    def close(self) -> None:
        self.db.close()

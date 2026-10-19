from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from radar.models import StateTransitionError, canonical_hash, validate_transition
from radar.operations import HttpStatusCheck, operation_catalog
from radar.schemas import CONFIG_DEFAULTS, validate_config
from radar.schemas.domain import DOMAIN_SCHEMAS, validate_closed_object
from radar.store import Scope, Store


NOW = "2026-09-16T10:00:00Z"


def test_fresh_database_applies_all_domain_migrations(tmp_path: Path):
    store = Store(tmp_path / "radar.sqlite3", Scope("profile-a", "owner-a"))

    status = store.status()

    assert status["schema_version"] == 11
    assert {
        "sources",
        "grants",
        "observations",
        "evidence",
        "goals",
        "followups",
        "followup_evidence",
        "plans",
        "proposals",
        "consents",
        "runs",
        "destinations",
        "outbox",
        "feedback",
        "audit",
        "budgets",
    } <= set(status["counts"])
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()


def test_same_database_never_leaks_rows_between_scopes(tmp_path: Path):
    path = tmp_path / "radar.sqlite3"
    store_a = Store(path, Scope("profile-a", "owner-a"))
    store_a.add_goal("goal-a", "Objetivo A", NOW)
    store_a.close()

    store_b = Store(path, Scope("profile-b", "owner-b"))
    store_b.add_goal("goal-b", "Objetivo B", NOW)

    assert [row["id"] for row in store_b.list_goals()] == ["goal-b"]
    with pytest.raises(KeyError):
        store_b.get_goal("goal-a")
    store_b.close()


def test_grant_check_is_fail_closed_and_bound_to_source_and_operation(tmp_path: Path):
    store = Store(tmp_path / "radar.sqlite3", Scope("profile-a", "owner-a"))
    store.add_source(
        source_id="source-1",
        kind="http_status",
        account_ref="fixture",
        resource_scope={"hosts": ["status.example.test"]},
        retention_days=30,
        now=NOW,
    )
    store.add_grant(
        grant_id="grant-1",
        permission="read",
        source_id="source-1",
        operation_ids=["http_status.check"],
        resource_scope={"hosts": ["status.example.test"]},
        destination_id=None,
        limits={"checks_per_day": 5},
        expires_at=None,
        now=NOW,
    )

    assert store.has_operation_grant("http_status.check", "source-1", NOW)
    assert not store.has_operation_grant("unknown.operation", "source-1", NOW)
    assert not store.has_operation_grant("http_status.check", "source-2", NOW)

    store.revoke_grant("grant-1", NOW)
    assert not store.has_operation_grant("http_status.check", "source-1", NOW)
    store.close()


def test_plan_hash_is_canonical_and_plan_rows_are_immutable(tmp_path: Path):
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})

    store = Store(tmp_path / "radar.sqlite3", Scope("profile-a", "owner-a"))
    store.add_goal("goal-1", "Comprobar servicio", NOW)
    store.add_followup(
        followup_id="followup-1",
        goal_id="goal-1",
        reason="condition_wait",
        status="ready",
        subject_key="service:fixture",
        evidence_revision=1,
        expires_at="2026-10-16T10:00:00Z",
        timezone="Europe/Madrid",
        confidence_band="high",
        assumptions={},
        stop_rule={"max_checks": 3},
        now=NOW,
    )
    first = store.add_plan(
        plan_id="plan-1",
        followup_id="followup-1",
        operation_id="http_status.check",
        operation_version="1",
        parameters={"url": "https://status.example.test/health", "timeout_seconds": 5},
        effect_class="read",
        evidence_revision=1,
        permission_revision=1,
        now=NOW,
    )
    same = store.add_plan(
        plan_id="plan-2",
        followup_id="followup-1",
        operation_id="http_status.check",
        operation_version="1",
        parameters={"timeout_seconds": 5, "url": "https://status.example.test/health"},
        effect_class="read",
        evidence_revision=1,
        permission_revision=1,
        now=NOW,
    )

    assert first["plan_hash"] == same["plan_hash"]
    assert same["id"] == "plan-1"
    store.close()


def test_state_machines_reject_unlisted_transitions():
    validate_transition("followup", "ready", "checking")
    validate_transition("proposal", "pending", "accepted")
    validate_transition("run", "queued", "running")
    validate_transition("outbox", "pending", "sending")

    with pytest.raises(StateTransitionError):
        validate_transition("followup", "completed", "ready")
    with pytest.raises(StateTransitionError):
        validate_transition("proposal", "pending", "succeeded")
    with pytest.raises(StateTransitionError):
        validate_transition("unknown-machine", "a", "b")


def test_failed_migration_rolls_back_schema_and_version(tmp_path: Path, monkeypatch):
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    (migration_dir / "001_ok.sql").write_text("CREATE TABLE ok_table (id INTEGER PRIMARY KEY);", encoding="utf-8")
    (migration_dir / "002_bad.sql").write_text("CREATE TABLE broken (;", encoding="utf-8")
    monkeypatch.setattr(Store, "migration_dir", migration_dir)

    with pytest.raises(sqlite3.DatabaseError):
        Store(tmp_path / "radar.sqlite3", Scope("profile-a", "owner-a"))

    db = sqlite3.connect(tmp_path / "radar.sqlite3")
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
    assert db.execute("SELECT name FROM sqlite_master WHERE name='broken'").fetchone() is None
    db.close()


def test_bootstrap_v2_database_upgrades_without_replaying_old_migrations(tmp_path: Path):
    path = tmp_path / "radar.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version(version) VALUES (1), (2);
        CREATE TABLE sources (
          id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
          kind TEXT NOT NULL, account_ref TEXT NOT NULL, resource_scope_json TEXT NOT NULL,
          enabled INTEGER NOT NULL, permission_revision INTEGER NOT NULL, cursor_json TEXT NOT NULL,
          last_success_at TEXT, retention_days INTEGER NOT NULL,
          PRIMARY KEY(profile_key, owner_id, id)
        );
        CREATE TABLE observations (
          id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
          source_id TEXT NOT NULL, source_event_key TEXT NOT NULL, occurred_at TEXT NOT NULL,
          received_at TEXT NOT NULL, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
          facts_json TEXT NOT NULL, expires_at TEXT, origin TEXT NOT NULL, correlation_id TEXT,
          PRIMARY KEY(profile_key, owner_id, id)
        );
        CREATE TABLE goals (
          id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
          title TEXT NOT NULL, status TEXT NOT NULL, scope_json TEXT NOT NULL,
          user_intent_ref TEXT, parent_goal_id TEXT,
          PRIMARY KEY(profile_key, owner_id, id)
        );
        CREATE TABLE followups (
          id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
          goal_id TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, subject_key TEXT NOT NULL,
          evidence_revision INTEGER NOT NULL, next_check_at TEXT, deadline_at TEXT, expires_at TEXT NOT NULL,
          timezone TEXT NOT NULL, confidence_band TEXT NOT NULL, assumptions_json TEXT NOT NULL,
          stop_rule_json TEXT NOT NULL, last_notified_at TEXT, reminder_count INTEGER NOT NULL,
          PRIMARY KEY(profile_key, owner_id, id)
        );
        CREATE TABLE audit (
          id TEXT NOT NULL, profile_key TEXT NOT NULL, owner_id TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
          event_type TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT,
          correlation_id TEXT, actor_kind TEXT NOT NULL, decision_code TEXT NOT NULL,
          metadata_redacted_json TEXT NOT NULL,
          PRIMARY KEY(profile_key, owner_id, id)
        );
        """
    )
    db.close()

    store = Store(path, Scope("profile-a", "owner-a"))
    assert store.status()["schema_version"] == 11
    assert {"grants", "evidence", "plans", "proposals", "destinations", "outbox", "budgets"} <= set(
        store.status()["counts"]
    )
    store.close()


def test_domain_schemas_are_closed_and_reject_unknown_fields():
    plan_schema = DOMAIN_SCHEMAS["plan"]
    assert plan_schema["additionalProperties"] is False
    assert {"operation_id", "operation_version", "parameters", "effect_class"} <= set(plan_schema["required"])

    ok, errors = validate_closed_object(
        "plan",
        {
            "operation_id": "http_status.check",
            "operation_version": "1",
            "parameters": {"url": "https://status.example.test/"},
            "effect_class": "read",
            "evidence_revision": 1,
            "permission_revision": 1,
            "unexpected": True,
        },
    )
    assert not ok
    assert errors == ["unknown fields: unexpected"]


def test_operation_catalog_exposes_policy_metadata_and_validates_parameters(monkeypatch):
    entry = operation_catalog()[0]
    assert entry == {
        "id": "http_status.check",
        "version": "1",
        "effect_class": "read",
        "allowed_resource_types": ["https_endpoint"],
        "required_grants": ["read"],
        "timeout_seconds": 10,
        "maximum_response_bytes": 4096,
        "budget_units": 1,
        "supports_idempotency": True,
        "supports_reconciliation": False,
    }

    called = False

    def fail(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be reached")

    monkeypatch.setattr("radar.operations.catalog.socket.create_connection", fail)
    result = HttpStatusCheck().execute(
        None,
        {"url": "https://status.example.test/", "unknown": True},
        {"allowlist": ["status.example.test"]},
    )
    assert result.status == "failed"
    assert result.data["error"] == "unknown parameters: unknown"
    assert called is False


def test_configuration_defaults_are_safe_and_complete():
    assert CONFIG_DEFAULTS["enabled"] is False
    assert CONFIG_DEFAULTS["mode"] == "shadow"
    assert CONFIG_DEFAULTS["destination"]["platform"] is None
    assert CONFIG_DEFAULTS["destination"]["verified"] is False
    assert CONFIG_DEFAULTS["notifications"]["enabled"] is False
    assert CONFIG_DEFAULTS["mutations"] == {"enabled": False, "require_per_action_consent": True}
    assert CONFIG_DEFAULTS["limits"]["checks_per_day"] == 200

    ok, errors = validate_config(CONFIG_DEFAULTS)
    assert ok, errors

    invalid = {**CONFIG_DEFAULTS, "destination": {**CONFIG_DEFAULTS["destination"], "verified": True}}
    ok, errors = validate_config(invalid)
    assert not ok
    assert "destination.verified is server-managed and cannot be true in configuration" in errors


def test_enabled_configuration_requires_owner_identity():
    configured = {
        **CONFIG_DEFAULTS,
        "enabled": True,
        "owner": {**CONFIG_DEFAULTS["owner"], "id": None},
    }
    ok, errors = validate_config(configured)
    assert not ok
    assert "owner.id is required when enabled" in errors


def test_followup_transition_is_revision_checked_and_scope_bound(tmp_path: Path):
    path = tmp_path / "radar.sqlite3"
    store = Store(path, Scope("profile-a", "owner-a"))
    store.add_goal("goal-1", "Comprobar servicio", NOW)
    store.add_followup(
        followup_id="followup-1",
        goal_id="goal-1",
        reason="condition_wait",
        status="ready",
        subject_key="service:fixture",
        evidence_revision=1,
        expires_at="2026-10-16T10:00:00Z",
        timezone="Europe/Madrid",
        confidence_band="high",
        assumptions={},
        stop_rule={"max_checks": 3},
        now=NOW,
    )

    revision = store.transition_state("followup", "followup-1", "ready", "checking", 1, NOW)
    assert revision == 2
    with pytest.raises(RuntimeError, match="revision conflict"):
        store.transition_state("followup", "followup-1", "checking", "ready", 1, NOW)
    with pytest.raises(StateTransitionError):
        store.transition_state("followup", "followup-1", "checking", "succeeded", 2, NOW)
    store.close()

    other = Store(path, Scope("profile-b", "owner-b"))
    with pytest.raises(KeyError):
        other.transition_state("followup", "followup-1", "checking", "ready", 2, NOW)
    other.close()

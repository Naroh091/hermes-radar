from __future__ import annotations

from pathlib import Path
import json

import radar
from radar.policy import Policy
from radar.store import Scope, Store


NOW = "2026-09-16T10:00:00Z"


def _store_with_http_grant(tmp_path: Path, operation_ids: list[str]) -> Store:
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
        operation_ids=operation_ids,
        resource_scope={"hosts": ["status.example.test"]},
        destination_id=None,
        limits={"checks_per_day": 5},
        expires_at=None,
        now=NOW,
    )
    return store


def test_policy_rejects_unknown_and_not_enabled_operations(tmp_path: Path):
    store = _store_with_http_grant(tmp_path, ["unknown.operation", "http_status.check"])
    policy = Policy(store, allowed_operations={"http_status.check"})

    unknown = policy.authorize_informational(
        "unknown.operation", "source-1", {"url": "https://status.example.test/"}, NOW
    )
    disabled = Policy(store, allowed_operations=set()).authorize_informational(
        "http_status.check", "source-1", {"url": "https://status.example.test/"}, NOW
    )

    assert (unknown.allowed, unknown.code) == (False, "unknown_operation")
    assert (disabled.allowed, disabled.code) == (False, "operation_not_enabled")
    store.close()


def test_policy_requires_resource_to_match_source_and_grant(tmp_path: Path):
    store = _store_with_http_grant(tmp_path, ["http_status.check"])
    policy = Policy(store, allowed_operations={"http_status.check"})

    denied = policy.authorize_informational(
        "http_status.check", "source-1", {"url": "https://other.example.test/health"}, NOW
    )
    allowed = policy.authorize_informational(
        "http_status.check", "source-1", {"url": "https://status.example.test/health"}, NOW
    )

    assert (denied.allowed, denied.code) == (False, "resource_not_granted")
    assert allowed.allowed is True
    assert allowed.code == "authorized"
    assert allowed.permission_revision == 1
    assert allowed.limits == {"checks_per_day": 5}
    store.close()


def test_policy_rejects_invalid_parameters_before_authorization(tmp_path: Path):
    store = _store_with_http_grant(tmp_path, ["http_status.check"])
    policy = Policy(store, allowed_operations={"http_status.check"})

    decision = policy.authorize_informational(
        "http_status.check",
        "source-1",
        {"url": "https://status.example.test/", "unexpected": True},
        NOW,
    )

    assert (decision.allowed, decision.code) == (False, "invalid_parameters")
    store.close()


def test_propose_tool_persists_authorized_immutable_plan(tmp_path: Path, monkeypatch):
    store = _store_with_http_grant(tmp_path, ["http_status.check"])
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

    class Context:
        profile_name = "profile-a"

        def get_config(self, key, default=None):
            values = {
                "enabled": True,
                "owner": {"id": "owner-a"},
                "allowed_operations": ["http_status.check"],
            }
            return values.get(key, default)

    context = Context()
    monkeypatch.setattr(radar, "_store", lambda ctx: store)
    payload = json.loads(
        radar.propose(
            context,
            {
                "followup_id": "followup-1",
                "source_id": "source-1",
                "operation_id": "http_status.check",
                "parameters": {"url": "https://status.example.test/health"},
            },
        )
    )

    assert payload["status"] == "plan_created"
    assert payload["executed"] is False
    assert payload["consent"] == "not_required_for_informational"
    assert store.status()["counts"]["plans"] == 1
    store.close()

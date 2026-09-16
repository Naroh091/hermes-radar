from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from proactive.operations import OPERATIONS, OperationResult
from proactive.scheduler import Scheduler, sync_configured_sources
from proactive.schemas import CONFIG_DEFAULTS, validate_config
from proactive.store import Scope, Store
from proactive.transports import DeliveryReceipt, TransportRegistry

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


def configured() -> dict:
    cfg = deepcopy(CONFIG_DEFAULTS)
    cfg.update(
        {
            "enabled": True,
            "owner": {"id": "owner-a", "timezone": "Europe/Madrid", "language": "es"},
            "allowed_operations": ["http_status.check"],
            "sources": [
                {
                    "id": "example-status",
                    "kind": "http_status",
                    "url": "https://status.example.test/health",
                    "interval_seconds": 300,
                    "retention_days": 7,
                    "enabled": True,
                    "operations": ["http_status.check"],
                }
            ],
        }
    )
    return cfg


def test_source_schema_is_closed_and_materialization_is_idempotent(tmp_path: Path):
    cfg = configured()
    ok, errors = validate_config(cfg)
    assert ok, errors
    store = Store(tmp_path / "db.sqlite3", Scope("profile-a", "owner-a"))

    first = sync_configured_sources(store, cfg, NOW)
    second = sync_configured_sources(store, cfg, NOW)

    assert first == second
    counts = store.status()["counts"]
    assert counts["sources"] == 1
    assert counts["grants"] == 1
    assert counts["goals"] == 1
    assert counts["followups"] == 1
    store.close()

    invalid = configured()
    invalid["sources"][0]["credential"] = "must-not-be-accepted"
    ok, errors = validate_config(invalid)
    assert not ok
    assert "sources[0] has unknown keys: credential" in errors


def test_scheduler_executes_authorized_check_once_and_persists_run(tmp_path: Path, monkeypatch):
    cfg = configured()
    store = Store(tmp_path / "db.sqlite3", Scope("profile-a", "owner-a"))
    sync_configured_sources(store, cfg, NOW)
    calls: list[dict] = []

    def fake_execute(scope, parameters, context=None):
        calls.append({"scope": scope, "parameters": parameters, "context": context})
        return OperationResult("succeeded", {"http_status": 204, "bytes_sampled": 0, "truncated": False})

    monkeypatch.setattr(OPERATIONS["http_status.check"], "execute", fake_execute)
    scheduler = Scheduler(store, cfg, worker_id="worker-1", clock=lambda: NOW)

    first = asyncio.run(scheduler.tick())
    second = asyncio.run(scheduler.tick())

    assert first == {"claimed": 1, "failed": 0, "recovered": 0, "delivered": 0}
    assert second == {"claimed": 0, "failed": 0, "recovered": 0, "delivered": 0}
    assert len(calls) == 1
    assert calls[0]["context"] == {"allowlist": ["status.example.test"]}
    assert store.status()["counts"]["plans"] == 1
    assert store.status()["counts"]["runs"] == 1
    run = store.db.execute("SELECT status, result_json FROM runs").fetchone()
    assert run["status"] == "succeeded"
    assert '"http_status":204' in run["result_json"]
    budget = store.db.execute("SELECT reserved_units, consumed_units FROM budgets").fetchone()
    assert tuple(budget) == (0, 1)
    followup = store.db.execute("SELECT status, next_check_at FROM followups").fetchone()
    assert tuple(followup) == ("ready", "2026-09-16T10:05:00Z")
    store.close()


def test_revoked_grant_fails_closed_before_network(tmp_path: Path, monkeypatch):
    cfg = configured()
    store = Store(tmp_path / "db.sqlite3", Scope("profile-a", "owner-a"))
    synced = sync_configured_sources(store, cfg, NOW)
    store.revoke_grant(synced[0]["grant_id"], "2026-09-16T10:00:00Z")
    monkeypatch.setattr(
        OPERATIONS["http_status.check"],
        "execute",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network must not run")),
    )

    result = asyncio.run(Scheduler(store, cfg, worker_id="worker-1", clock=lambda: NOW).tick())

    assert result["claimed"] == 1
    assert store.status()["counts"]["runs"] == 0
    assert store.db.execute("SELECT status FROM followups").fetchone()[0] == "failed"
    assert store.db.execute("SELECT decision_code FROM audit").fetchone()[0] == "grant_missing_or_inactive"
    store.close()


def test_active_mode_delivers_unhealthy_result_through_verified_outbox(tmp_path: Path, monkeypatch):
    cfg = configured()
    cfg["mode"] = "active"
    cfg["notifications"]["enabled"] = True
    cfg["destination"].update(
        {
            "platform": "telegram",
            "gateway_connection_ref": "telegram:default",
            "address": {"chat_id": "42"},
            "owner_platform_id": "7",
        }
    )
    store = Store(tmp_path / "db.sqlite3", Scope("profile-a", "owner-a"))
    sync_configured_sources(store, cfg, NOW)
    store.verify_destination(
        destination_id="configured:primary", platform="telegram",
        gateway_connection_ref="telegram:default", address={"chat_id": "42"},
        owner_platform_id="7", kind="private", capabilities={"text": True},
        now="2026-09-16T10:00:00Z",
    )
    monkeypatch.setattr(
        OPERATIONS["http_status.check"], "execute",
        lambda *_a, **_k: OperationResult(
            "succeeded", {"http_status": 503, "bytes_sampled": 0, "truncated": False}
        ),
    )
    sent: list[tuple[dict, dict]] = []
    transports = TransportRegistry()

    async def sender(destination, payload):
        sent.append((destination, payload))
        return DeliveryReceipt("message-1")

    transports.register("telegram", sender)
    result = asyncio.run(
        Scheduler(store, cfg, worker_id="worker-1", clock=lambda: NOW, transports=transports).tick()
    )

    assert result["delivered"] == 1
    assert sent[0][0]["address"] == {"chat_id": "42"}
    assert "503" in sent[0][1]["text"]
    outbox = store.db.execute("SELECT status, platform_message_id, attempt FROM outbox").fetchone()
    assert tuple(outbox) == ("sent", "message-1", 1)
    store.close()

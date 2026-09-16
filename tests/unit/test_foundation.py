import json
import sqlite3
from pathlib import Path

import pytest

from proactive.operations import HttpStatusCheck
from proactive.schemas import validate_config
from proactive.store import Scope, Store


def test_config_rejects_unknown_keys_and_invalid_mode():
    ok, errors = validate_config({"enabled": False, "mode": "live", "unexpected": True})
    assert not ok
    assert any("unknown keys" in e for e in errors)
    assert any("mode" in e for e in errors)


def test_store_migrates_and_keeps_scope_isolated(tmp_path: Path):
    db = Store(tmp_path / "proactive.sqlite3", Scope("profile-a", "owner-a"))
    db.add_goal("goal-1", "Comprobar servicio", "2026-09-16T00:00:00Z")
    assert db.status()["schema_version"] == 6
    assert db.status()["counts"]["goals"] == 1
    db.close()
    other = Store(tmp_path / "other.sqlite3", Scope("profile-b", "owner-b"))
    assert other.list_followups() == []
    other.close()


def test_http_operation_rejects_non_https_and_non_allowlisted_without_network(monkeypatch):
    called = False
    def fail(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be reached")
    monkeypatch.setattr("proactive.operations.catalog.socket.create_connection", fail)
    result = HttpStatusCheck().execute(None, {"url": "http://example.test/", "timeout_seconds": 1}, {"allowlist": ["example.test"]})
    assert result.status == "failed"
    assert "HTTPS" in result.data["error"]
    assert called is False


def test_http_operation_rejects_private_target_before_request(monkeypatch):
    monkeypatch.setattr("proactive.operations.catalog.socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    result = HttpStatusCheck().execute(None, {"url": "https://example.test/"}, {"allowlist": ["example.test"]})
    assert result.status == "failed"
    assert "private or local" in result.data["error"]

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from radar.schemas import CONFIG_DEFAULTS
from radar.store import Scope, Store

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
SLOTS = ["09:00", "14:00", "19:00"]  # the suite was written against fixed slots; paced reminders set digest_times: []


def config(**overrides: Any) -> dict:
    cfg = deepcopy(CONFIG_DEFAULTS)
    cfg.update({"enabled": True, "owner": {"id": "owner-a", "timezone": "Europe/Madrid", "language": "es"}})
    cfg["observation"]["local_sessions"] = True
    cfg["notifications"]["digest_times"] = list(SLOTS)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    return cfg


def active_config(**overrides: Any) -> dict:
    cfg = config()
    cfg["mode"] = "active"
    cfg["notifications"] = {**cfg["notifications"], "enabled": True, "daily_limit": 10, "quiet_start": "00:00", "quiet_end": "00:00"}
    cfg["destination"] = {**cfg["destination"], "platform": "telegram", "gateway_connection_ref": "telegram:default",
                          "address": {"chat_id": "42"}, "owner_platform_id": "7"}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    return cfg


class FakeLlm:
    """Returns canned parsed answers per purpose, in order."""

    def __init__(self, answers: dict[str, list[dict]]):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls: list[dict] = []

    def complete_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        purpose = kwargs.get("purpose", "")
        queue = self.answers.get(purpose) or []
        if not queue:
            raise RuntimeError(f"no canned answer for {purpose}")
        parsed = queue.pop(0)
        return SimpleNamespace(parsed=parsed, text="", model="fake", provider="fake", usage=SimpleNamespace(input_tokens=10, output_tokens=5, cost_usd=0.0))


class FakeContext:
    profile_name = "profile-a"

    def __init__(self, cfg: dict, llm: Any = None, call_mcp: Any = None):
        self.cfg = cfg
        self.llm = llm
        self.call_mcp = call_mcp
        self.spawned: list[Any] = []
        self.settings: dict[str, Any] = {}

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.cfg.get(key, default)

    def set_config(self, key: str, value: Any) -> None:
        self.settings[key] = value

    def spawn_task(self, coroutine: Any, name: str | None = None) -> Any:
        self.spawned.append(name)
        return asyncio.create_task(coroutine, name=name)


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "db.sqlite3", Scope("profile-a", "owner-a"))
    yield db
    db.close()


def verify_destination(store: Store, now: str = "2026-10-19T10:00:00Z") -> None:
    store.verify_destination(
        destination_id="configured:primary", platform="telegram", gateway_connection_ref="telegram:default",
        address={"chat_id": "42"}, owner_platform_id="7", kind="private", capabilities={"text": True, "buttons": True}, now=now,
    )


def extract_answer(**overrides: Any) -> dict:
    base = {
        "relevant": True, "category": "trip", "title": "Vuelo a Lisboa 3-5 nov", "summary": "Billete de ida confirmado.",
        "disposition": "notify_now", "reason": "Falta alojamiento", "urgency": "medium", "impact": "medium", "risk_flags": [],
        "entities": {"dates": [{"label": "ida", "when": "2026-11-03"}], "places": ["Lisboa"], "amounts": [], "people": [],
                     "organizations": ["TAP"], "identifiers": [{"label": "PNR", "value": "ABC123"}], "items": [], "repo": "", "url": ""},
        "expectations": [{"description": "reserva de hotel", "kind": "email", "by": "2026-11-01"}],
        "facts_learned": [{"key": "shoe size", "value": "42", "confidence": 0.9}],
        "proposed_reply": "", "keep_full_content": False, "needs_thread": False,
    }
    base.update(overrides)
    return base


def plan_answer(**overrides: Any) -> dict:
    base = {
        "task_match": {"decision": "new", "task_id": "", "why": "no candidates"},
        "task": {"kind": "trip", "title": "Viaje a Lisboa 3-5 nov", "summary": "Ida confirmada, falta hotel y vuelta.",
                 "status": "open", "due_at": "2026-11-03T06:00:00Z", "next_review_at": "2026-11-02T06:00:00Z", "confidence": 0.8},
        "checklist": [{"item": "outbound transport booked", "status": "done", "note": "TAP ABC123"},
                      {"item": "lodging booked for every night of the stay", "status": "missing", "note": ""}],
        "checks": [],
        "proposals": [
            {"title": "Buscar hotel en Lisboa 3-5 nov", "rationale": "No hay reserva", "action_class": "read", "operation": "web.read",
             "when": "", "urgency": "normal", "options": [{"id": "do", "label": "Sí, busca"}, {"id": "no", "label": "No"}]},
            {"title": "Hacer check-in online", "rationale": "Abre 24h antes", "action_class": "local", "operation": "",
             "when": "2026-11-02T06:00:00Z", "urgency": "normal", "options": [{"id": "do", "label": "Recuérdamelo"}]},
        ],
        "notify": {"disposition": "notify_now", "reason": "gap with deadline"},
        "message": "Tienes vuelo a Lisboa el 3 de noviembre y aún no hay hotel.",
    }
    base.update(overrides)
    return base

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

try:
    from hermes_constants import get_hermes_home
except ImportError:  # permite pruebas unitarias fuera del entorno Hermes
    def get_hermes_home():
        return Path.home() / ".hermes"

from .config import load_config
from .operations import OPERATIONS, operation_catalog
from .schemas import tools
from .store import Scope, Store

log = logging.getLogger(__name__)
_RUNTIME = {}


def _store(ctx) -> Store:
    key = id(ctx)
    if key not in _RUNTIME:
        profile = getattr(ctx, "profile_name", "default") or "default"
        cfg = load_config(ctx)
        owner = str(cfg["owner"].get("id", "owner-unconfigured"))
        path = get_hermes_home() / "proactive" / "proactive.sqlite3"
        _RUNTIME[key] = Store(path, Scope(profile, owner))
    return _RUNTIME[key]["store"] if isinstance(_RUNTIME[key], dict) else _RUNTIME[key]


def status(ctx, **kwargs) -> str:
    try:
        cfg = load_config(ctx); store = _store(ctx)
        return tools.as_json({"enabled": cfg["enabled"], "mode": cfg["mode"], "scheduler": {"configured": True, "running": bool(_RUNTIME.get(id(ctx), {}).get("task")) if isinstance(_RUNTIME.get(id(ctx)), dict) else False}, "store": store.status(), "operations": operation_catalog()})
    except Exception as exc: return tools.as_json({"error": str(exc)})


def list_followups(ctx, args=None, **kwargs) -> str:
    try: return tools.as_json({"followups": _store(ctx).list_followups((args or {}).get("limit", 50))})
    except Exception as exc: return tools.as_json({"error": str(exc)})


def inspect(ctx, args=None, **kwargs) -> str:
    # Detailed object lookup is intentionally not exposed until typed repositories exist.
    return tools.as_json({"error": "inspect is limited to typed objects; unsupported or missing object"})


def propose(ctx, args=None, **kwargs) -> str:
    args = args or {}; op_id = args.get("operation_id")
    if op_id not in OPERATIONS: return tools.as_json({"error": "unknown operation; catalog is closed"})
    # This MVP only exposes safe informational checks. No tool can create consent or a mutation.
    if OPERATIONS[op_id].effect_class != "informational": return tools.as_json({"error": "mutation requires authenticated consent"})
    return tools.as_json({"status": "candidate_validated", "operation_id": op_id, "consent": "not_required_for_informational", "executed": False})


async def _ticker(ctx, interval: int):
    while True:
        await asyncio.sleep(interval)


def on_session_start(ctx, **kwargs):
    try:
        cfg = load_config(ctx)
        if not cfg["enabled"]: return
        if not hasattr(asyncio, "get_running_loop"): return
        task = ctx.spawn_task(_ticker(ctx, max(1, int(cfg["scheduler"].get("tick_seconds", 60)))), name="proactive:scheduler")
        _RUNTIME[id(ctx)] = {"store": _store(ctx), "task": task}
    except (RuntimeError, ValueError, TypeError) as exc:
        log.warning("proactive scheduler unavailable: %s", exc)


def cleanup(ctx):
    runtime = _RUNTIME.pop(id(ctx), None)
    if isinstance(runtime, dict):
        runtime.get("store", None) and runtime["store"].close()


def register(ctx):
    """Entry point pública de Hermes; no importa ni altera el core."""
    ctx.register_tool(name="proactive_status", toolset="proactive", schema=tools.STATUS, handler=lambda a, **k: status(ctx, **k))
    ctx.register_tool(name="proactive_list", toolset="proactive", schema=tools.LIST, handler=lambda a, **k: list_followups(ctx, a, **k))
    ctx.register_tool(name="proactive_inspect", toolset="proactive", schema=tools.INSPECT, handler=lambda a, **k: inspect(ctx, a, **k))
    ctx.register_tool(name="proactive_propose", toolset="proactive", schema=tools.PROPOSE, handler=lambda a, **k: propose(ctx, a, **k))
    ctx.register_hook("on_session_start", lambda **kw: on_session_start(ctx, **kw))
    ctx.on_unload(lambda: cleanup(ctx))

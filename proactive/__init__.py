from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

try:
    from hermes_constants import get_hermes_home
except ImportError:  # permite pruebas unitarias fuera del entorno Hermes
    def get_hermes_home():
        return Path.home() / ".hermes"

from .config import load_config
from .detectors import detect
from .models import canonical_hash
from .operations import OPERATIONS, operation_catalog
from .policy import Policy
from .scheduler import Scheduler, iso, sync_configured_sources, utc_now
from .schemas import tools
from .store import Scope, Store
from .transports import TransportRegistry, telegram_sender

log = logging.getLogger(__name__)
_RUNTIME = {}
_TRANSPORTS: dict[int, TransportRegistry] = {}


def _store(ctx) -> Store:
    key = id(ctx)
    if key not in _RUNTIME:
        profile = getattr(ctx, "profile_name", None)
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError("reliable profile identity is required")
        cfg = load_config(ctx)
        owner = cfg["owner"].get("id")
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("configured owner identity is required")
        path = get_hermes_home() / "proactive" / "proactive.sqlite3"
        _RUNTIME[key] = Store(path, Scope(profile, owner))
    return _RUNTIME[key]["store"] if isinstance(_RUNTIME[key], dict) else _RUNTIME[key]


def status(ctx, **kwargs) -> str:
    try:
        cfg = load_config(ctx)
        owner_id = cfg["owner"].get("id")
        if not cfg["enabled"]:
            return tools.as_json(
                {
                    "enabled": False,
                    "configured": isinstance(owner_id, str) and bool(owner_id.strip()),
                    "mode": cfg["mode"],
                    "scheduler": {"configured": True, "running": False},
                    "store": None,
                    "operations": operation_catalog(),
                }
            )
        store = _store(ctx)
        runtime = _RUNTIME.get(id(ctx))
        return tools.as_json(
            {
                "enabled": True,
                "configured": True,
                "mode": cfg["mode"],
                "scheduler": {
                    "configured": True,
                    "running": bool(
                        isinstance(runtime, dict)
                        and runtime.get("task") is not None
                        and not runtime["task"].done()
                    ),
                },
                "store": store.status(),
                "operations": operation_catalog(),
            }
        )
    except Exception as exc:
        return tools.as_json({"error": str(exc)})


def list_followups(ctx, args=None, **kwargs) -> str:
    try: return tools.as_json({"followups": _store(ctx).list_followups((args or {}).get("limit", 50))})
    except Exception as exc: return tools.as_json({"error": str(exc)})


def inspect(ctx, args=None, **kwargs) -> str:
    try:
        object_id = (args or {}).get("id")
        if not isinstance(object_id, str) or not object_id:
            return tools.as_json({"error": "id_required"})
        return tools.as_json({"object": _store(ctx).inspect_object(object_id)})
    except KeyError:
        return tools.as_json({"error": "object_not_found"})
    except Exception as exc:
        return tools.as_json({"error": str(exc)})


def propose(ctx, args=None, **kwargs) -> str:
    try:
        args = args or {}
        cfg = load_config(ctx)
        if not cfg["enabled"]:
            return tools.as_json({"error": "plugin_disabled"})
        operation_id = args.get("operation_id")
        if operation_id not in OPERATIONS:
            return tools.as_json({"error": "unknown_operation"})
        operation = OPERATIONS[operation_id]
        if operation.effect_class != "informational":
            return tools.as_json({"error": "authenticated_consent_required"})
        followup_id = args.get("followup_id")
        source_id = args.get("source_id")
        parameters = args.get("parameters")
        if not all(isinstance(value, str) and value for value in (followup_id, source_id)) or not isinstance(parameters, dict):
            return tools.as_json({"error": "invalid_arguments"})
        store = _store(ctx)
        followup = store.get_followup(followup_id)
        if followup["status"] not in {"ready", "checking"}:
            return tools.as_json({"error": "followup_not_ready"})
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        decision = Policy(store, cfg["allowed_operations"]).authorize_informational(
            operation_id, source_id, parameters, now
        )
        if not decision.allowed:
            return tools.as_json({"error": decision.code})
        plan = store.add_plan(
            plan_id=str(uuid4()),
            followup_id=followup_id,
            operation_id=operation_id,
            operation_version=operation.version,
            parameters=parameters,
            effect_class=operation.effect_class,
            evidence_revision=followup["evidence_revision"],
            permission_revision=decision.permission_revision or 0,
            now=now,
            source_id=source_id,
        )
        return tools.as_json(
            {
                "status": "plan_created" if plan["created"] else "plan_reused",
                "plan_id": plan["id"],
                "plan_hash": plan["plan_hash"],
                "operation_id": operation_id,
                "consent": "not_required_for_informational",
                "executed": False,
            }
        )
    except KeyError:
        return tools.as_json({"error": "followup_not_found"})
    except Exception as exc:
        log.warning("proactive proposal rejected: %s", exc)
        return tools.as_json({"error": "proposal_rejected"})


async def _ticker(ctx, scheduler: Scheduler, interval: int):
    while True:
        try:
            await scheduler.tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("proactive scheduler tick failed: %s", exc)
        await asyncio.sleep(interval)


def capture_observation(ctx, kind: str, **event) -> None:
    """Persist bounded hook metadata only; never raw prompts, tool args, or tool results."""
    try:
        cfg = load_config(ctx)
        if not cfg["enabled"] or not cfg["observation"]["local_sessions"]:
            return
        now_dt = utc_now()
        now = iso(now_dt)
        store = _store(ctx)
        source_id = store.ensure_local_observation_source(now, cfg["retention"]["observations_days"])
        allowed = {
            "tool_name", "status", "duration_ms", "error_type", "session_id",
            "parent_session_id", "tool_call_id", "turn_id", "model", "provider",
            "input_tokens", "output_tokens", "subagent_id",
        }
        facts = {
            key: value for key, value in event.items()
            if key in allowed and isinstance(value, (str, int, float, bool, type(None)))
        }
        stable = {
            "kind": kind,
            "tool_call_id": facts.get("tool_call_id"),
            "turn_id": facts.get("turn_id"),
            "subagent_id": facts.get("subagent_id"),
            "session_id": facts.get("session_id"),
            "status": facts.get("status"),
        }
        event_key = canonical_hash(stable)
        store.add_observation(
            observation_id=str(uuid4()), source_id=source_id, source_event_key=event_key,
            occurred_at=now, received_at=now, kind=kind,
            fingerprint=canonical_hash({"kind": kind, "facts": facts}), facts=facts,
            origin="plugin", expires_at=iso(now_dt + timedelta(days=cfg["retention"]["observations_days"])),
            correlation_id=facts.get("turn_id") or facts.get("tool_call_id"),
        )
        for candidate in detect(kind, event, now_dt):
            store.upsert_detected_followup(
                followup_id=str(uuid4()), reason=candidate.reason, status=candidate.status,
                subject_key=candidate.subject_key,
                next_check_at=iso(candidate.next_check_at) if candidate.next_check_at else None,
                expires_at=iso(now_dt + timedelta(days=30)),
                timezone=cfg["owner"]["timezone"], confidence_band=candidate.confidence_band,
                assumptions=candidate.assumptions, now=now,
            )
    except Exception as exc:
        log.warning("proactive observation skipped: %s", exc)


def on_session_start(ctx, **kwargs):
    try:
        cfg = load_config(ctx)
        if not cfg["enabled"]: return
        current = _RUNTIME.get(id(ctx))
        if isinstance(current, dict):
            current_task = current.get("task")
            if current_task is not None and not current_task.done():
                return
        store = _store(ctx)
        sync_configured_sources(store, cfg)
        worker_id = f"{getattr(ctx, 'profile_name', 'unknown')}:{id(ctx)}"
        transports = _TRANSPORTS.setdefault(id(ctx), TransportRegistry())
        scheduler = Scheduler(store, cfg, worker_id=worker_id, transports=transports)
        coroutine = _ticker(ctx, scheduler, max(1, int(cfg["scheduler"].get("tick_seconds", 60))))
        try:
            task = ctx.spawn_task(coroutine, name="proactive:scheduler")
        except Exception:
            coroutine.close()
            _RUNTIME.pop(id(ctx), None)
            store.close()
            raise
        _RUNTIME[id(ctx)] = {"store": store, "task": task, "scheduler": scheduler}
    except Exception as exc:
        log.warning("proactive scheduler unavailable: %s", exc)


def cleanup(ctx):
    runtime = _RUNTIME.pop(id(ctx), None)
    if isinstance(runtime, dict):
        task = runtime.get("task")
        if task is not None and not task.done():
            task.cancel()
        store = runtime.get("store")
        if store is not None:
            store.close()
    elif runtime is not None:
        runtime.close()
    _TRANSPORTS.pop(id(ctx), None)


def telegram_factory(ctx):
    """Create a narrowly scoped Telegram verification handler and outbound sender."""
    def factory(native, adapter):
        try:
            from telegram.ext import CommandHandler
        except ImportError:
            log.warning("Telegram SDK unavailable; proactive transport not registered")
            return
        registry = _TRANSPORTS.setdefault(id(ctx), TransportRegistry())
        registry.register("telegram", telegram_sender(native.bot))

        async def verify(update, context):
            message = getattr(update, "effective_message", None)
            try:
                cfg = load_config(ctx)
                destination = cfg["destination"]
                user = getattr(update, "effective_user", None)
                chat = getattr(update, "effective_chat", None)
                expected_actor = destination.get("owner_platform_id")
                expected_chat = destination.get("address", {}).get("chat_id")
                if not cfg["enabled"] or destination.get("platform") != "telegram":
                    raise PermissionError("Proactive no está configurado para Telegram.")
                if user is None or str(user.id) != str(expected_actor):
                    raise PermissionError("Actor no autorizado.")
                if chat is None or str(chat.id) != str(expected_chat):
                    raise PermissionError("Destino distinto del configurado.")
                kind = "private" if getattr(chat, "type", "") == "private" else "group"
                if kind == "group" and not destination.get("allow_groups"):
                    raise PermissionError("Los grupos no están autorizados.")
                now = iso(utc_now())
                _store(ctx).verify_destination(
                    destination_id="configured:primary", platform="telegram",
                    gateway_connection_ref=destination.get("gateway_connection_ref") or "telegram:default",
                    address={"chat_id": str(chat.id)}, owner_platform_id=str(user.id), kind=kind,
                    capabilities={"text": True, "buttons": True}, now=now,
                )
                if message is not None:
                    await message.reply_text("Destino de Proactive verificado.")
            except PermissionError as exc:
                if message is not None:
                    await message.reply_text(str(exc))
            except Exception:
                log.exception("proactive Telegram verification failed")
                if message is not None:
                    await message.reply_text("No se pudo verificar el destino.")

        native.add_handler(CommandHandler("proactive_verify", verify), group=-1)

    return factory


def register(ctx):
    """Entry point pública de Hermes; no importa ni altera el core."""
    ctx.register_tool(name="proactive_status", toolset="proactive", schema=tools.STATUS, handler=lambda a, **k: status(ctx, **k))
    ctx.register_tool(name="proactive_list", toolset="proactive", schema=tools.LIST, handler=lambda a, **k: list_followups(ctx, a, **k))
    ctx.register_tool(name="proactive_inspect", toolset="proactive", schema=tools.INSPECT, handler=lambda a, **k: inspect(ctx, a, **k))
    ctx.register_tool(name="proactive_propose", toolset="proactive", schema=tools.PROPOSE, handler=lambda a, **k: propose(ctx, a, **k))
    ctx.register_hook("on_session_start", lambda **kw: on_session_start(ctx, **kw))
    ctx.register_hook("post_tool_call", lambda **kw: capture_observation(ctx, "tool_call", **kw))
    ctx.register_hook("post_llm_call", lambda **kw: capture_observation(ctx, "llm_call", **kw))
    ctx.register_hook("subagent_stop", lambda **kw: capture_observation(ctx, "subagent_stop", **kw))
    ctx.register_platform_handler("telegram", telegram_factory(ctx))
    from .cli import command as cli_command, setup_cli
    ctx.register_cli_command(
        name="proactive", help="Inspect and run the proactive scheduler",
        setup_fn=setup_cli, handler_fn=lambda args: cli_command(ctx, args),
        description="Validate configuration, inspect scoped state, or run one safe cycle.",
    )
    ctx.on_unload(lambda: cleanup(ctx))

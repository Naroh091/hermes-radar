from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

try:
    from hermes_constants import get_hermes_home
except ImportError:  # unit tests outside the Hermes environment
    def get_hermes_home():
        return Path.home() / ".hermes"

from .config import load_config
from .delivery.responses import respond_to_proposal
from .delivery.telegram_ui import authorize_update, command_text, handle_callback, handle_text_reply, session_identity
from .i18n import t
from .jobs.firewall import guard_tool_call
from .jobs.service import start_or_wait
from .llm import register_aux_tasks
from .operations import OPERATIONS, operation_catalog
from .policy import Policy
from .render.report import render_task_report
from .runtime import Runtime
from .scheduler import Scheduler, iso, sync_configured_sources, utc_now
from .schemas import tools
from .sources.session import record_subagent, record_turn
from .store import Scope, Store
from .transports import TransportRegistry, telegram_editor, telegram_sender

log = logging.getLogger(__name__)
_RUNTIME: dict[int, dict] = {}
_TRANSPORTS: dict[int, TransportRegistry] = {}


def _store(ctx) -> Store:
    key = id(ctx)
    entry = _RUNTIME.get(key)
    if entry is not None:
        return entry["store"] if isinstance(entry, dict) else entry
    profile = getattr(ctx, "profile_name", None)
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("reliable profile identity is required")
    cfg = load_config(ctx)
    owner = cfg["owner"].get("id")
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("configured owner identity is required")
    path = get_hermes_home() / "radar" / "radar.sqlite3"
    store = Store(path, Scope(profile, owner))
    _RUNTIME[key] = store
    return store


def _runtime(ctx) -> Runtime:
    key = id(ctx)
    entry = _RUNTIME.get(key)
    if isinstance(entry, dict) and entry.get("runtime") is not None:
        return entry["runtime"]
    cfg = load_config(ctx)
    store = _store(ctx)
    transports = _TRANSPORTS.setdefault(key, TransportRegistry())
    worker_id = f"{getattr(ctx, 'profile_name', 'unknown')}:{key}"
    runtime = Runtime(ctx, store, cfg, worker_id=worker_id, transports=transports, hermes_home=str(get_hermes_home()))
    _RUNTIME[key] = {"store": store, "runtime": runtime, "task": None, "scheduler": runtime.scheduler}
    return runtime


JOB_CHILD_ENV = "HERMES_RADAR_JOB"


def _in_job_child() -> bool:
    """Delegated ``hermes chat`` sessions must not observe themselves or start a second scheduler."""
    return bool(os.environ.get(JOB_CHILD_ENV))


def _enabled(ctx) -> dict | None:
    if _in_job_child():
        return None
    try:
        cfg = load_config(ctx)
    except Exception as exc:
        log.warning("radar configuration invalid: %s", exc)
        return None
    return cfg if cfg["enabled"] else None


# -- tools ----------------------------------------------------------------------------------------
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
        entry = _RUNTIME.get(id(ctx))
        runtime = entry.get("runtime") if isinstance(entry, dict) else None
        task = entry.get("task") if isinstance(entry, dict) else None
        return tools.as_json(
            {
                "enabled": True,
                "configured": True,
                "mode": cfg["mode"],
                "trust": cfg["trust"]["level"],
                "scheduler": {"configured": True, "running": bool(task is not None and not task.done())},
                "runtime": runtime.status() if runtime is not None else None,
                "store": store.status(),
                "pending": {
                    "tasks": len(store.list_tasks(statuses=("open", "waiting", "needs_user", "in_progress", "snoozed"), limit=500)),
                    "proposals": len(store.list_proposals(statuses=("pending", "sent", "snoozed"), limit=500)),
                    "waiting": len(store.list_proposals(statuses=("waiting",), limit=500)),
                    "signals": store.pending_signal_count(),
                    "jobs": len(store.list_jobs(statuses=("queued", "running"), limit=50)),
                },
                "operations": operation_catalog(),
            }
        )
    except Exception as exc:
        return tools.as_json({"error": str(exc)})


def list_followups(ctx, args=None, **kwargs) -> str:
    try:
        return tools.as_json({"followups": _store(ctx).list_followups((args or {}).get("limit", 50))})
    except Exception as exc:
        return tools.as_json({"error": str(exc)})


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


def tasks_tool(ctx, args=None, **kwargs) -> str:
    try:
        args = args or {}
        store = _store(ctx)
        which = args.get("status", "open")
        statuses = {
            "open": ("open", "waiting", "needs_user", "in_progress", "snoozed"),
            "closed": ("done", "abandoned", "expired"),
        }.get(which)
        rows = []
        for task in store.list_tasks(statuses=statuses, limit=int(args.get("limit", 20))):
            proposals = store.list_proposals(statuses=("pending", "sent", "snoozed", "waiting", "executing"), task_id=task["id"], limit=10)
            rows.append(
                {
                    "id": task["id"], "kind": task["kind"], "title": task["title"], "status": task["status"],
                    "summary": task.get("summary"), "due_at": task.get("due_at"), "next_review_at": task.get("next_review_at"),
                    "missing": [c.get("item") for c in task.get("checklist") or [] if isinstance(c, dict) and c.get("status") == "missing"],
                    "proposals": [
                        {"id": p["id"], "title": p["title"], "action_class": p["action_class"], "status": p["status"],
                         "options": [o.get("id") for o in p.get("options") or [] if isinstance(o, dict)]}
                        for p in proposals
                    ],
                }
            )
        return tools.as_json({"tasks": rows})
    except Exception as exc:
        return tools.as_json({"error": str(exc)})


def resolve_tool(ctx, args=None, **kwargs) -> str:
    try:
        args = args or {}
        cfg = load_config(ctx)
        if not cfg["enabled"]:
            return tools.as_json({"error": "plugin_disabled"})
        runtime = _runtime(ctx)
        runtime.refresh_config()
        option = {"accept": "do", "decline": "no", "later": "later", "done": "done"}.get(str(args.get("decision")))
        if option is None or not isinstance(args.get("proposal_id"), str):
            return tools.as_json({"error": "invalid_arguments"})
        result = respond_to_proposal(
            runtime.store, cfg, proposal_id=args["proposal_id"], option=option, actor="agent", now_dt=utc_now(),
            channel="tool", note=str(args.get("note") or ""),
        )
        job = None
        blocked = None
        proposal = result.get("proposal") or {}
        if result.get("outcome") == "accepted" and proposal and proposal.get("operation_id") != "check":
            job, blocked = start_or_wait(runtime.store, cfg, proposal=proposal, option=option, now_dt=utc_now())
        return tools.as_json({"outcome": result.get("outcome"), "text": result.get("text"), "job_id": job["id"] if job else None,
                              "waiting_reason": blocked})
    except KeyError:
        return tools.as_json({"error": "proposal_not_found"})
    except Exception as exc:
        return tools.as_json({"error": str(exc)})


def preference_tool(ctx, args=None, **kwargs) -> str:
    """Store a preference the owner stated in the chat; the agent supplies effect and scope."""
    try:
        args = args or {}
        cfg = load_config(ctx)
        if not cfg["enabled"]:
            return tools.as_json({"error": "plugin_disabled"})
        from .delivery.learn import preference_from_fields, store_preference

        runtime = _runtime(ctx)
        data = preference_from_fields(args)
        record = store_preference(runtime.store, data, origin="explicit", now_dt=utc_now(), feedback_ref="agent")
        runtime.refresh_preferences()
        return tools.as_json({"saved": record})
    except ValueError as exc:
        return tools.as_json({"error": f"invalid_arguments: {exc}"})
    except Exception as exc:
        return tools.as_json({"error": str(exc)})


def explain_tool(ctx, args=None, **kwargs) -> str:
    try:
        args = args or {}
        cfg = load_config(ctx)
        store = _store(ctx)
        task_id = args.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return tools.as_json({"error": "task_id_required"})
        task = store.get_task(task_id)
        signals = store.signals_for_task(task_id, limit=20)
        proposals = store.list_proposals(task_id=task_id, limit=20)
        jobs = store.list_jobs(task_id=task_id, limit=10)
        payload = {
            "task": task,
            "signals": [{k: v for k, v in s.items() if k != "raw_content"} for s in signals],
            "proposals": proposals,
            "jobs": [{k: v for k, v in j.items() if k != "prompt"} for j in jobs],
        }
        if args.get("html"):
            reports = get_hermes_home() / "radar" / "reports"
            reports.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = reports / f"{task_id}.html"
            path.write_text(
                render_task_report(task, signals=signals, proposals=proposals, facts=store.list_facts(limit=20), jobs=jobs,
                                   language=cfg["owner"].get("language", "es")),
                encoding="utf-8",
            )
            payload["html_path"] = str(path)
        return tools.as_json(payload)
    except KeyError:
        return tools.as_json({"error": "task_not_found"})
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
        if operation.effect_class != "read":
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
        log.warning("radar proposal rejected: %s", exc)
        return tools.as_json({"error": "proposal_rejected"})


# -- scheduler lifecycle -----------------------------------------------------------------------
async def _ticker(ctx, scheduler: Scheduler, interval: int):
    while True:
        try:
            await scheduler.tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("radar scheduler tick failed: %s", exc)
        await asyncio.sleep(interval)


async def _receiver_task(runtime: Runtime):
    try:
        await runtime.start_receiver()
    except Exception as exc:
        log.warning("radar gmail push receiver failed to start: %s", exc)


def on_session_start(ctx, **kwargs):
    if _in_job_child():
        return
    try:
        cfg = load_config(ctx)
        if not cfg["enabled"]:
            return
        current = _RUNTIME.get(id(ctx))
        if isinstance(current, dict):
            current_task = current.get("task")
            if current_task is not None and not current_task.done():
                return
        store = _store(ctx)
        sync_configured_sources(store, cfg)
        runtime = _runtime(ctx)
        try:
            runtime.reload_sources()
        except Exception as exc:
            log.warning("radar sources not loaded: %s", exc)
        coroutine = _ticker(ctx, runtime.scheduler, max(1, int(cfg["scheduler"].get("tick_seconds", 60))))
        try:
            task = ctx.spawn_task(coroutine, name="radar:scheduler")
        except RuntimeError as exc:
            # CLI/TUI sessions have no running loop: the scheduler only lives in the gateway.
            coroutine.close()
            _RUNTIME.pop(id(ctx), None)
            store.close()
            if "no running event loop" in str(exc):
                log.debug("radar scheduler not started here (no event loop); the gateway runs it")
                return
            raise
        except Exception:
            coroutine.close()
            _RUNTIME.pop(id(ctx), None)
            store.close()
            raise
        runtime.task = task
        _RUNTIME[id(ctx)] = {"store": store, "runtime": runtime, "task": task, "scheduler": runtime.scheduler}
        if cfg["ingress"]["gmail_receiver"].get("enabled") and runtime.receiver is None:
            try:
                ctx.spawn_task(_receiver_task(runtime), name="radar:gmail-receiver")
            except Exception as exc:
                log.warning("radar receiver not started: %s", exc)
    except Exception as exc:
        log.warning("radar scheduler unavailable: %s", exc)


def cleanup(ctx):
    entry = _RUNTIME.pop(id(ctx), None)
    if isinstance(entry, dict):
        task = entry.get("task")
        if task is not None and not task.done():
            task.cancel()
        runtime = entry.get("runtime")
        if runtime is not None:
            for job_task in list(runtime.job_tasks):
                job_task.cancel()
            if runtime.receiver is not None:
                try:
                    asyncio.get_running_loop().create_task(runtime.receiver.stop())
                except RuntimeError:
                    pass
        store = entry.get("store")
        if store is not None:
            store.close()
    elif entry is not None:
        entry.close()
    _TRANSPORTS.pop(id(ctx), None)


# -- hooks --------------------------------------------------------------------------------------
def capture_turn(ctx, **event) -> None:
    try:
        cfg = _enabled(ctx)
        if cfg is None or not cfg["observation"]["local_sessions"]:
            return
        record_turn(_store(ctx), cfg, event, utc_now())
    except Exception as exc:
        log.warning("radar turn not recorded: %s", exc)


def capture_subagent(ctx, **event) -> None:
    try:
        cfg = _enabled(ctx)
        if cfg is None or not cfg["observation"]["local_sessions"]:
            return
        record_subagent(_store(ctx), cfg, event, utc_now())
    except Exception as exc:
        log.warning("radar subagent result not recorded: %s", exc)


async def on_gateway_event(ctx, event=None, **kwargs):
    try:
        cfg = _enabled(ctx)
        if cfg is None or event is None:
            return None
        runtime = _runtime(ctx)
        runtime.refresh_config()
        directive = runtime.consumer.on_event(event)
        if directive:
            return directive
        return await handle_text_reply(runtime, event)
    except Exception as exc:
        log.warning("radar gateway hook failed: %s", exc)
        return None


def pending_context(ctx, **kwargs):
    try:
        cfg = _enabled(ctx)
        if cfg is None or str(kwargs.get("platform") or "") in {"cron", "webhook"}:
            return None
        store = _store(ctx)
        from .trust import needs_consent

        lines = ["[radar] Pending decisions the owner may be referring to (resolve them with the radar_resolve tool):"]
        for proposal in store.list_proposals(statuses=("pending", "sent"), limit=60, newest=True):
            task = None
            if proposal.get("task_id"):
                try:
                    task = store.get_task(proposal["task_id"])
                except KeyError:
                    task = None
            if not needs_consent(cfg, task, proposal):
                continue  # the assistant's own queued work is not something the owner answers
            task_title = (task or {}).get("title", "")
            lines.append(f"- {proposal['id']}: {proposal.get('title', '')}" + (f" (task: {task_title})" if task_title else ""))
            if len(lines) > 8:
                break
        if len(lines) == 1:
            return None
        text = "\n".join(lines)
        return {"context": text[:1500]}
    except Exception as exc:
        log.debug("radar context skipped: %s", exc)
        return None


def command(ctx, raw_args: str = "", **kwargs) -> str:
    try:
        cfg = _enabled(ctx)
        if cfg is None:
            return "Radar is disabled."
        runtime = _runtime(ctx)
        runtime.refresh_config()

        def set_trust(level: str) -> None:
            setter = getattr(ctx, "set_config", None)
            if callable(setter):
                setter("trust_level", level)
            runtime.config["trust_level"] = level

        return command_text(runtime, raw_args, set_trust=set_trust, identity=session_identity())
    except Exception as exc:
        return f"radar: {exc}"


# -- telegram -------------------------------------------------------------------------------------
def telegram_factory(ctx):
    """Register the outbound sender and the button callbacks on the host's Telegram application."""
    def factory(native, adapter):
        try:
            from telegram.ext import CallbackQueryHandler
        except ImportError:
            log.warning("Telegram SDK unavailable; radar buttons not registered")
            CallbackQueryHandler = None
        registry = _TRANSPORTS.setdefault(id(ctx), TransportRegistry())
        registry.register("telegram", telegram_sender(native.bot), telegram_editor(native.bot))

        async def callback(update, context):
            try:
                runtime = _runtime(ctx)
                runtime.refresh_config()
            except Exception as exc:
                log.warning("radar callback without runtime: %s", exc)
                return
            await handle_callback(runtime, update, context)

        # Verification runs through the host command registry (``/radar verify``): the gateway answers
        # unknown slash commands itself, so a PTB CommandHandler never sees them.
        if CallbackQueryHandler is not None:
            native.add_handler(CallbackQueryHandler(callback, pattern=r"^p[at]:"), group=-1)
        on_session_start(ctx)

    return factory


def register(ctx):
    """Public Hermes entry point; it never imports or alters the core."""
    ctx.register_tool(name="radar_status", toolset="radar", schema=tools.STATUS, handler=lambda a, **k: status(ctx, **k))
    ctx.register_tool(name="radar_tasks", toolset="radar", schema=tools.TASKS, handler=lambda a, **k: tasks_tool(ctx, a, **k))
    ctx.register_tool(name="radar_resolve", toolset="radar", schema=tools.RESOLVE, handler=lambda a, **k: resolve_tool(ctx, a, **k))
    ctx.register_tool(name="radar_explain", toolset="radar", schema=tools.EXPLAIN, handler=lambda a, **k: explain_tool(ctx, a, **k))
    ctx.register_tool(name="radar_list", toolset="radar", schema=tools.LIST, handler=lambda a, **k: list_followups(ctx, a, **k))
    ctx.register_tool(name="radar_inspect", toolset="radar", schema=tools.INSPECT, handler=lambda a, **k: inspect(ctx, a, **k))
    ctx.register_tool(name="radar_propose", toolset="radar", schema=tools.PROPOSE, handler=lambda a, **k: propose(ctx, a, **k))
    ctx.register_tool(name="radar_preference", toolset="radar", schema=tools.PREFERENCE, handler=lambda a, **k: preference_tool(ctx, a, **k))
    ctx.register_hook("on_session_start", lambda **kw: on_session_start(ctx, **kw))
    ctx.register_hook("post_llm_call", lambda **kw: capture_turn(ctx, **kw))
    ctx.register_hook("subagent_stop", lambda **kw: capture_subagent(ctx, **kw))
    ctx.register_hook("pre_gateway_dispatch", lambda **kw: on_gateway_event(ctx, **kw))
    ctx.register_hook("pre_llm_call", lambda **kw: pending_context(ctx, **kw))
    ctx.register_hook("pre_tool_call", lambda **kw: guard_tool_call(**kw))  # inert outside a job child
    ctx.register_platform_handler("telegram", telegram_factory(ctx))
    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        # ``/radar`` is a built-in alias of ``/loop`` in Hermes, so the in-session command is ``/radar``.
        register_command("radar", lambda raw_args="", **kw: command(ctx, raw_args, **kw),
                         description="Radar tasks: status | list | next | prefs | forget <id> | pause | resume | mute <id> | trust <level>", args_hint="<subcommand>")
    register_aux_tasks(ctx)
    from .cli import command as cli_command, setup_cli
    ctx.register_cli_command(
        name="radar", help="Set up, inspect and drive the radar plugin",
        setup_fn=setup_cli, handler_fn=lambda args: cli_command(ctx, args),
        description="Discover MCP servers, validate configuration, sync sources, list tasks or run one cycle.",
    )
    ctx.on_unload(lambda: cleanup(ctx))

"""Radar panel backend, mounted at ``/api/plugins/radar/`` (the web dashboard and Hermes Desktop share it).

Runs inside the dashboard/serve process, not the gateway: it reads and writes the same SQLite store the
gateway uses. Decisions taken here (accept, decline, snooze) are recorded exactly like a Telegram button;
the gateway's scheduler starts the approved work on its next tick.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from radar.config import _merge, _normalize  # noqa: E402
from radar.delivery.context import thread_context  # noqa: E402
from radar.delivery.learn import describe_preference, forget_preference, preference_from_fields, store_preference  # noqa: E402
from radar.delivery.notice import DRAFT_INLINE  # noqa: E402
from radar.delivery.responses import OPEN_PROPOSAL, act_on_task, respond_to_proposal, review_detail  # noqa: E402
from radar.jobs.service import start_or_wait  # noqa: E402
from radar.models import TASK_TERMINAL  # noqa: E402
from radar.patterns import describe as describe_pattern, dismiss as dismiss_pattern  # noqa: E402
from radar.render.report import render_task_report  # noqa: E402
from radar.schemas import CONFIG_DEFAULTS  # noqa: E402
from radar.store import Scope, Store  # noqa: E402
from radar.trust import describe as describe_trust  # noqa: E402

log = logging.getLogger(__name__)
router = APIRouter()

_IN_FLIGHT = ("pending", "sent", "snoozed", "accepted", "waiting", "executing")
_store_cache: dict[tuple[str, str, str], Store] = {}


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _settings() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config as host_load  # type: ignore[import-not-found]

        raw = ((host_load() or {}).get("plugins") or {}).get("entries", {}).get("radar", {}).get("settings") or {}
    except Exception as exc:
        log.warning("radar settings not read from the host config: %s", exc)
    merged = deepcopy(CONFIG_DEFAULTS)
    for key, default in CONFIG_DEFAULTS.items():
        if key in raw:
            merged[key] = _merge(default, raw[key])
    _normalize(merged)
    return merged


def _profile() -> str:
    return str(os.environ.get("HERMES_PROFILE") or "default")


def _open() -> tuple[Store, dict[str, Any]]:
    config = _settings()
    owner = str((config.get("owner") or {}).get("id") or "")
    if not owner:
        raise HTTPException(status_code=503, detail="radar is not configured (owner.id missing)")
    path = _hermes_home() / "radar" / "radar.sqlite3"
    key = (str(path), _profile(), owner)
    store = _store_cache.get(key)
    if store is None:
        if not path.exists():
            raise HTTPException(status_code=503, detail="radar store not found; the gateway has not run yet")
        store = Store(path, Scope(_profile(), owner))
        _store_cache[key] = store
    return store, config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _proposal_view(proposal: dict[str, Any]) -> dict[str, Any]:
    params = proposal.get("params") or {}
    draft = str(params.get("draft") or "")
    return {
        "id": proposal["id"], "title": proposal.get("title"), "reason": proposal.get("reason_text") or params.get("rationale") or "",
        "action_class": proposal.get("action_class"), "operation": proposal.get("operation_id"), "status": proposal.get("status"),
        "urgency": proposal.get("urgency"), "options": [o for o in proposal.get("options") or [] if isinstance(o, dict)],
        "draft": draft[:DRAFT_INLINE], "draft_truncated": len(draft) > DRAFT_INLINE, "draft_ref": params.get("draft_ref") or "",
        "wait_reason": params.get("wait_reason") or "", "created_at": proposal.get("created_at"), "responded_at": proposal.get("responded_at"),
        "message": params.get("message") or "",
    }


def _task_view(store: Store, config: dict[str, Any], task: dict[str, Any], *, now_dt: datetime, with_proposals: bool = True) -> dict[str, Any]:
    entities = task.get("entities") or {}
    view = {
        "id": task["id"], "kind": task.get("kind"), "title": task.get("title"), "summary": task.get("summary"), "status": task.get("status"),
        "due_at": task.get("due_at"), "next_review_at": task.get("next_review_at"), "muted": bool(task.get("muted")), "updated_at": task.get("updated_at"),
        "created_at": task.get("created_at"), "confidence": task.get("confidence"),
        "checklist": [c for c in task.get("checklist") or [] if isinstance(c, dict)],
        "entities": {k: v for k, v in entities.items() if k in ("repo", "pr", "branch", "pattern_id", "schedule_cron") and v},
        "draft": (entities.get("draft") or {}).get("text", "")[:DRAFT_INLINE] if isinstance(entities.get("draft"), dict) else "",
        "context": thread_context(store, task["id"], config, now_dt=now_dt),
        "jobs_running": len(store.list_jobs(statuses=("queued", "running"), task_id=task["id"], limit=5)),
    }
    if with_proposals:
        proposals = [p for p in store.list_proposals(task_id=task["id"], limit=30) if p.get("status") in _IN_FLIGHT]
        view["proposals"] = [_proposal_view(p) for p in proposals]
    return view


@router.get("/status")
def status() -> dict[str, Any]:
    store, config = _open()
    state = store.status()
    return {"counts": state.get("counts", {}), "schema_version": state.get("schema_version"), "mode": config.get("mode"),
            "trust": describe_trust(str((config.get("trust") or {}).get("level") or "balanced")), "language": (config.get("owner") or {}).get("language", "es"),
            "profile": _profile()}


@router.get("/board")
def board() -> dict[str, Any]:
    store, config = _open()
    now_dt = _now()
    open_statuses = tuple(s for s in ("open", "waiting", "needs_user", "in_progress", "snoozed") if s not in TASK_TERMINAL)
    rows = store.list_tasks(statuses=open_statuses, limit=100)
    tasks = [_task_view(store, config, t, now_dt=now_dt) for t in rows if not t.get("muted")]
    muted_tasks = [{"id": t["id"], "kind": t.get("kind"), "title": t.get("title"), "status": t.get("status"), "updated_at": t.get("updated_at")} for t in rows if t.get("muted")]
    order = {"needs_user": 0, "in_progress": 1, "open": 2, "waiting": 3, "snoozed": 4}
    tasks.sort(key=lambda t: (order.get(str(t["status"]), 9), -len(t.get("proposals") or []), str(t.get("due_at") or "9")))
    language = str((config.get("owner") or {}).get("language") or "es")
    patterns = [{**{k: p.get(k) for k in ("id", "kind", "name", "description", "status", "support", "confidence", "task_id", "automation_ref", "last_seen_at")},
                 "summary": describe_pattern(p, language), "evidence": p.get("evidence") or {}} for p in store.list_patterns(limit=50)]
    preferences = [{**{k: p.get(k) for k in ("id", "dimension", "effect", "scope", "exceptions", "instruction", "origin", "version")}, "summary": describe_preference(p, language)}
                   for p in store.list_preferences(active_only=True)]
    counts = store.status().get("counts", {})
    stats = {
        "tasks_open": len(tasks), "proposals_pending": sum(1 for t in tasks for p in t["proposals"] if p["status"] in ("pending", "sent")),
        "proposals_waiting": sum(1 for t in tasks for p in t["proposals"] if p["status"] == "waiting"),
        "jobs_running": len(store.list_jobs(statuses=("queued", "running"), limit=20)), "signals_pending": store.pending_signal_count(),
        "patterns": len(patterns), "preferences": len(preferences), "counts": counts, "muted": len(muted_tasks),
    }
    return {"generated_at": _iso(now_dt), "language": language, "mode": config.get("mode"), "trust_level": (config.get("trust") or {}).get("level"),
            "tasks": tasks, "muted_tasks": muted_tasks, "patterns": patterns, "preferences": preferences, "stats": stats}


@router.get("/tasks/{task_id}")
def task_detail(task_id: str) -> dict[str, Any]:
    store, config = _open()
    try:
        task = store.get_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="task not found")
    now_dt = _now()
    view = _task_view(store, config, task, now_dt=now_dt)
    view["history"] = [_proposal_view(p) for p in store.list_proposals(task_id=task_id, limit=30)]
    view["signals"] = [
        {"id": s.get("id"), "kind": s.get("kind"), "occurred_at": s.get("occurred_at"), "title": s.get("title"), "snippet": (s.get("snippet") or "")[:600],
         "role": s.get("role"), "meta": {k: v for k, v in (s.get("meta") or {}).items() if k in ("sender", "account", "repo", "event_type", "action", "number", "url", "start", "end", "location", "status", "change")}}
        for s in store.signals_for_task(task_id, limit=12)
    ]
    view["jobs"] = [
        {"id": j.get("id"), "kind": j.get("kind"), "status": j.get("status"), "action_class": j.get("action_class"), "created_at": j.get("created_at"),
         "finished_at": j.get("finished_at"), "summary": j.get("result_summary") or "", "error": j.get("error") or "",
         "effects": list((j.get("result") or {}).get("effects") or [])[:10], "refs": list((j.get("result") or {}).get("refs") or [])[:10]}
        for j in store.list_jobs(task_id=task_id, limit=10)
    ]
    view["entities_full"] = task.get("entities") or {}
    view["expectations"] = task.get("expectations") or []
    return view


@router.get("/tasks/{task_id}/report")
def task_report(task_id: str) -> dict[str, Any]:
    store, config = _open()
    try:
        task = store.get_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="task not found")
    html = render_task_report(task, signals=store.signals_for_task(task_id, limit=20), proposals=store.list_proposals(task_id=task_id, limit=20),
                              facts=store.list_facts(limit=20), jobs=store.list_jobs(task_id=task_id, limit=10),
                              language=str((config.get("owner") or {}).get("language") or "es"))
    return {"html": html, "data_url": "data:text/html;base64," + base64.b64encode(html.encode("utf-8")).decode("ascii")}


class RespondBody(BaseModel):
    option: str
    note: str = ""


@router.post("/proposals/{proposal_id}/respond")
def respond(proposal_id: str, body: RespondBody) -> dict[str, Any]:
    store, config = _open()
    now_dt = _now()
    result = respond_to_proposal(store, config, proposal_id=proposal_id, option=body.option, actor="desktop", now_dt=now_dt, channel="desktop", note=body.note)
    payload: dict[str, Any] = {"outcome": result.get("outcome"), "text": result.get("text"), "detail": result.get("detail")}
    proposal = result.get("proposal") or {}
    if result.get("outcome") == "accepted" and proposal and proposal.get("operation_id") != "check":
        job, reason = start_or_wait(store, config, proposal=proposal, option=body.option, now_dt=now_dt)
        payload.update({"job_id": job["id"] if job else None, "waiting_reason": reason})
    return payload


@router.get("/proposals/{proposal_id}/detail")
def proposal_detail(proposal_id: str) -> dict[str, Any]:
    store, config = _open()
    try:
        proposal = store.get_proposal(proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    return {"text": review_detail(store, config, proposal), "open": proposal.get("status") in OPEN_PROPOSAL}


class TaskActionBody(BaseModel):
    action: str


@router.post("/tasks/{task_id}/action")
def task_action(task_id: str, body: TaskActionBody) -> dict[str, Any]:
    if body.action not in ("later", "mute", "done", "unmute"):
        raise HTTPException(status_code=400, detail="action must be later, mute, unmute or done")
    store, config = _open()
    if body.action == "unmute":
        try:
            store.update_task(task_id, now=_iso(_now()), bump_version=False, muted=False)
        except KeyError:
            raise HTTPException(status_code=404, detail="task not found")
        return {"outcome": "unmuted", "text": ""}
    result = act_on_task(store, config, task_id=task_id, action=body.action, actor="desktop", now_dt=_now())
    return {"outcome": result.get("outcome"), "text": result.get("text")}


class PreferenceBody(BaseModel):
    instruction: str
    effect: str
    dimension: str = "notification"
    scope: dict[str, str] = {}
    exceptions: list[str] = []


@router.get("/preferences")
def preferences() -> dict[str, Any]:
    store, config = _open()
    language = str((config.get("owner") or {}).get("language") or "es")
    return {"preferences": [{**{k: p.get(k) for k in ("id", "effect", "scope", "instruction", "version", "status")}, "summary": describe_preference(p, language)}
                            for p in store.list_preferences(active_only=True)]}


@router.post("/preferences")
def add_preference(body: PreferenceBody) -> dict[str, Any]:
    store, config = _open()
    try:
        data = preference_from_fields(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    record = store_preference(store, data, origin="explicit", now_dt=_now(), feedback_ref="desktop")
    return {"saved": record, "summary": describe_preference(record, str((config.get("owner") or {}).get("language") or "es"))}


@router.post("/preferences/{preference_id}/forget")
def forget(preference_id: str) -> dict[str, Any]:
    store, _ = _open()
    record = forget_preference(store, preference_id, now_dt=_now())
    if record is None:
        raise HTTPException(status_code=404, detail="preference not found")
    return {"disabled": record["id"]}


@router.post("/patterns/{pattern_id}/dismiss")
def dismiss(pattern_id: str) -> dict[str, Any]:
    store, _ = _open()
    try:
        pattern = store.get_pattern(pattern_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="pattern not found")
    now = _iso(_now())
    if not dismiss_pattern(store, pattern.get("task_id"), now):
        store.set_pattern_status(pattern_id, "dismissed", now)
    for proposal in store.list_proposals(task_id=pattern.get("task_id"), limit=10) if pattern.get("task_id") else []:
        if proposal.get("status") in OPEN_PROPOSAL:
            try:
                store.respond_proposal(proposal["id"], decision="declined", response={"option": "no_automate", "actor": "desktop", "channel": "desktop"}, now=now)
            except Exception:
                pass
    return {"dismissed": pattern_id}


def _ws_upgrade_authorized(ws: WebSocket) -> bool:
    """The dashboard's canonical WS gate (``?token=``/``?ticket=``); accepted when the dashboard is not importable (tests)."""
    try:
        from hermes_cli import web_server_chat as _ws  # type: ignore[import-not-found]
    except Exception:
        return True
    return bool(_ws._ws_auth_ok(ws))


def _change_marker(store: Store) -> str:
    try:
        row = store.db.execute(
            "SELECT COALESCE((SELECT MAX(updated_at) FROM tasks), '') || '|' || COALESCE((SELECT MAX(updated_at) FROM proposals), '')"
            " || '|' || COALESCE((SELECT MAX(updated_at) FROM jobs), '') || '|' || COALESCE((SELECT MAX(updated_at) FROM patterns), '')"
            " || '|' || (SELECT COUNT(*) FROM proposals) || '|' || (SELECT COUNT(*) FROM tasks)"
        ).fetchone()
        return str(row[0] if row else "")
    except Exception:
        return ""


@router.websocket("/events")
async def events(ws: WebSocket) -> None:
    if not _ws_upgrade_authorized(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        store, _ = _open()
    except HTTPException as exc:
        await ws.send_json({"type": "error", "detail": exc.detail})
        await ws.close()
        return
    last = await asyncio.to_thread(_change_marker, store)
    quiet = 0
    try:
        while True:
            await asyncio.sleep(2)
            marker = await asyncio.to_thread(_change_marker, store)
            if marker != last:
                last = marker
                quiet = 0
                await ws.send_json({"type": "changed", "at": _iso(_now())})
            else:
                quiet += 1
                if quiet >= 10:
                    quiet = 0
                    await ws.send_json({"type": "ping", "at": _iso(_now())})
    except (WebSocketDisconnect, RuntimeError):
        return

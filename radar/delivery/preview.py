"""The next notice: preview it (``hermes radar next --dry-run``) or launch it for real (``/radar next``).

The scheduler holds non-urgent messages during quiet hours and spaces
reminders over the waking day, so late at night there is no way to know
what is waiting — and no way to ask for it now. :func:`preview_next`
answers what is next without touching anything; :func:`deliver_next`
launches it: the queued outbox message first, then an immediate notice
ready to enqueue, then the next reminder. An explicit owner request
crosses quiet hours (payload ``urgent``), the daily cap and the pause;
everything else — the thread re-read, withdrawn proposals, steps
scheduled for a later hour — still applies at delivery time, on the next
scheduler tick. The reminder text comes from the template fallback: this
path never calls the model.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..i18n import t
from ..models import canonical_hash
from ..trust import needs_consent
from .cadence import URGENCIES, next_delivery, next_morning, paced, waking_window
from .context import thread_context
from .dispatch import (
    DESTINATION_ID,
    _decision_like,
    _deliverable,
    _group_by_task,
    _in_progress,
    _parse,
    _pressing,
    _rank_key,
    _urgent,
    build_reminder,
    draft_document,
    iso as _iso,
    next_reminder_at,
)
from .notice import DRAFT_INLINE, build_notice, fit
from .remind import fallback_text

_TAG = re.compile(r"<[^>]+>")
QUEUED_STATUSES = ("pending", "sending", "failed", "unknown")
QUEUED_KINDS = ("notice", "digest")
_SOON = timedelta(minutes=1)


def plain(value: Any, limit: int = 1500) -> str:
    """Outbox and notice texts are Telegram HTML; the command answer is plain text."""
    text = html.unescape(str(value or ""))
    text = _TAG.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return fit(text, limit)


def quiet_until(config: dict[str, Any], now_dt: datetime) -> datetime | None:
    """End of the quiet hours holding ``now_dt`` back, or None when awake."""
    try:
        start, _ = waking_window(now_dt, config)
        if start.tzinfo is None:
            return None
        return start if start > now_dt.astimezone(start.tzinfo) else None
    except Exception:
        return None


def _sent_today(store: Any, now_dt: datetime) -> int:
    start_of_day = now_dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return store.notification_count_since(_iso(start_of_day))


def _daily_limit(config: dict[str, Any]) -> int:
    try:
        return max(1, int((config.get("notifications") or {}).get("daily_limit", 8)))
    except (TypeError, ValueError):
        return 8


def _pending_outbox(store: Any) -> list[dict[str, Any]]:
    rows = [row for row in store.list_outbox(limit=200)
            if row.get("status") in QUEUED_STATUSES and row.get("kind") in QUEUED_KINDS]
    rows.sort(key=lambda row: (str(row.get("next_attempt_at") or row.get("created_at") or ""),
                               str(row.get("created_at") or ""), str(row.get("id") or "")))
    return rows


def preview_next(store: Any, config: dict[str, Any], now_dt: datetime, *, paused: bool = False) -> dict[str, Any]:
    """The next aviso as a plain dict, ignoring quiet hours to show it.

    ``held`` is None when the message would go out on the next tick, or
    ``quiet_hours`` / ``daily_cap`` / ``paused`` / the raw outbox error when
    something holds it; ``when`` is the expected delivery moment (None only
    when paused before anything was enqueued).
    """
    now_iso = _iso(now_dt)
    if config.get("mode") != "active" or not (config.get("notifications") or {}).get("enabled"):
        return {"kind": "inactive", "reason": "mode", "mode": config.get("mode"), "text": ""}
    try:
        store.destination(DESTINATION_ID)
    except KeyError:
        return {"kind": "inactive", "reason": "no_destination", "text": ""}
    language = (config.get("owner") or {}).get("language", "es")
    held_quiet = quiet_until(config, now_dt)
    limit = _daily_limit(config)

    queued = _pending_outbox(store)
    if queued:
        row = queued[0]
        payload = row.get("payload") or {}
        urgent = bool(payload.get("urgent"))
        held = row.get("last_error") or None
        when = _parse(row.get("next_attempt_at")) or now_dt
        if held_quiet is not None and not urgent:
            held = held or "quiet_hours"
            if held == "quiet_hours" and when < held_quiet:
                when = held_quiet
        elif urgent:
            held, when = None, min(when, now_dt)
        return {
            "kind": "queued", "task_id": row.get("task_id") or payload.get("task_id"),
            "text": plain(payload.get("text")),
            "proposal_ids": list(payload.get("proposal_ids") or []) + list(payload.get("news_ids") or []),
            "outbox_id": row.get("id"), "outbox_kind": row.get("kind"),
            "when": _iso(when), "held": held,
            "quiet_until": _iso(held_quiet) if held_quiet else None,
            "urgent": urgent, "backlog": 0,
        }

    pending = [p for p in store.list_proposals(statuses=("pending",), limit=200) if _deliverable(p, now_iso)]
    groups = _group_by_task(store, [p for p in pending if p.get("batch_key") == "now"])
    if groups:
        task, items = min(groups, key=lambda pair: min(_rank_key(pair[0], p) for p in pair[1]))
        message = next((p.get("params", {}).get("message") for p in items if p.get("params", {}).get("message")), "")
        payload = build_notice(task, items, language=language, message=message,
                               context=thread_context(store, task["id"], config, now_dt=now_dt))
        urgent = any(_urgent(p) for p in items)
        exempt = any((p.get("params") or {}).get("owner_followup") for p in items)
        capped = not exempt and _sent_today(store, now_dt) >= limit
        if paused:
            held, when = "paused", None
        elif capped:
            held, when = "daily_cap", next_morning(now_dt, config)
        elif held_quiet is not None and not urgent:
            held, when = "quiet_hours", held_quiet
        else:
            held, when = None, now_dt
        return {
            "kind": "notice", "task_id": task["id"],
            "text": plain(payload.get("text")),
            "proposal_ids": [p["id"] for p in items],
            "when": _iso(when) if when else None, "held": held,
            "quiet_until": _iso(held_quiet) if held_quiet else None,
            "urgent": urgent, "backlog": 0,
        }

    decisions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    news: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for task, items in _group_by_task(store, [p for p in pending if p.get("batch_key") == "digest"]):
        for proposal in items:
            if needs_consent(config, task, proposal):
                decisions.append((task, proposal))
            elif proposal.get("operation_id") == "inform":
                news.append((task, proposal))
    if not decisions and not news:
        return {"kind": "empty", "text": ""}
    sel = _order_digest(store, config, now_dt, decisions, news)
    if paused:
        held, when = "paused", None
    elif _sent_today(store, now_dt) >= limit:  # reminders never bypass the daily cap
        held, when = "daily_cap", next_morning(now_dt, config)
    elif held_quiet is not None and (sel["when"] is None or sel["when"] <= now_dt + _SOON):
        held, when = "quiet_hours", held_quiet
    else:
        held, when = None, sel["when"]
    return {
        "kind": "reminder", "task_id": (sel["items"][0].get("task") or {}).get("id"),
        "text": fallback_text(sel["items"], language),
        "proposal_ids": sel["proposal_ids"],
        "when": _iso(when) if when else None, "held": held,
        "quiet_until": _iso(held_quiet) if held_quiet else None,
        "urgent": False, "backlog": sel["backlog"],
    }


def _order_digest(store: Any, config: dict[str, Any], now_dt: datetime,
                  decisions: list[tuple[dict[str, Any], dict[str, Any]]],
                  news: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    """Best reminder selection ignoring due-ness: the most pressing decisions first."""
    count = max(1, int((config.get("notifications") or {}).get("reminder_items", 1) or 1))
    if paced(config):
        regular = next_reminder_at(store, config, now_dt, waiting=len(decisions) or 1)
        pressing = {proposal["id"]: moment for task, proposal in decisions
                    if regular is not None and (moment := _pressing(task, proposal, config, now_dt, regular)) is not None}
        decisions.sort(key=lambda pair: (pair[1]["id"] not in pressing, pressing.get(pair[1]["id"], now_dt), *_rank_key(*pair)))
        when = regular
    else:
        decisions.sort(key=lambda pair: (_slot_key(pair[1], config, now_dt), *_rank_key(*pair)))
        when = _slot_key(decisions[0][1], config, now_dt)
    news.sort(key=lambda pair: _rank_key(*pair))
    chosen = decisions[:count]
    if chosen:
        chosen_news = [pair for pair in news if pair[0]["id"] == chosen[0][0]["id"]][:1]
        items = [{"task": task, "decision": proposal, "news": [n for t_, n in chosen_news if t_["id"] == task["id"]]}
                 for task, proposal in chosen]
    else:
        chosen_news = news[:1]
        items = [{"task": chosen_news[0][0], "decision": None, "news": [chosen_news[0][1]]}]
    if paced(config) and when is not None:
        moments = [pressing.get(proposal["id"]) for _, proposal in chosen if proposal["id"] in pressing]
        moments = [m for m in moments if m is not None]
        if moments:
            when = min([when] + moments)
    unanswered = [p for p in store.list_proposals(statuses=("sent",), limit=200) if _decision_like(p)]
    backlog = len(decisions) - len(chosen) + len(unanswered)
    listed = [entry["decision"]["id"] for entry in items if entry.get("decision")]
    listed += [p["id"] for p in ([n for entry in items for n in entry.get("news") or []])]
    return {"items": items, "proposal_ids": listed, "backlog": max(0, backlog), "when": when}


def _task_title(store: Any, task_id: Any) -> str:
    try:
        return str(store.get_task(str(task_id)).get("title") or "")
    except (KeyError, AttributeError):
        return ""


def deliver_next(store: Any, config: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    """Launch the next aviso for real, ignoring quiet hours.

    An explicit owner request crosses quiet hours (payload ``urgent``), the
    daily cap and the pause; the message itself goes out on the next
    scheduler tick, where the thread re-read and the withdrawn-proposal
    checks still apply. Steps scheduled for a later hour (``deliver_after``)
    are never launched early.
    """
    now_iso = _iso(now_dt)
    if config.get("mode") != "active" or not (config.get("notifications") or {}).get("enabled"):
        return {"kind": "inactive", "reason": "mode", "mode": config.get("mode")}
    try:
        store.destination(DESTINATION_ID)
    except KeyError:
        return {"kind": "inactive", "reason": "no_destination"}
    language = (config.get("owner") or {}).get("language", "es")
    quiet = quiet_until(config, now_dt) is not None

    queued = _pending_outbox(store)
    waiting = [row for row in queued if row.get("status") in ("pending", "failed", "unknown")]
    if waiting:
        row = waiting[0]
        payload = store.force_outbox(row["id"], now=now_iso)["payload"]
        task_id = row.get("task_id") or payload.get("task_id")
        return {
            "kind": "queued", "task_id": task_id, "title": _task_title(store, task_id),
            "proposal_ids": list(payload.get("proposal_ids") or []) + list(payload.get("news_ids") or []),
            "outbox_id": row["id"], "quiet": quiet, "backlog": 0,
        }
    if any(row.get("status") == "sending" for row in queued):
        return {"kind": "sending"}

    pending = [p for p in store.list_proposals(statuses=("pending",), limit=200) if _deliverable(p, now_iso)]
    groups = _group_by_task(store, [p for p in pending if p.get("batch_key") == "now"])
    if groups:
        task, items = min(groups, key=lambda pair: min(_rank_key(pair[0], p) for p in pair[1]))
        message = next((p.get("params", {}).get("message") for p in items if p.get("params", {}).get("message")), "")
        payload = build_notice(task, items, language=language, message=message,
                               context=thread_context(store, task["id"], config, now_dt=now_dt))
        payload["urgent"] = True  # the owner asked for it now: it crosses quiet hours
        payload["exempt"] = any((p.get("params") or {}).get("owner_followup") for p in items)
        draft = next((p.get("params", {}).get("draft") for p in items if p.get("params", {}).get("draft")), "")
        if draft and len(draft) > DRAFT_INLINE:
            document = draft_document(task["id"], draft, language=language, now_dt=now_dt)
            if document:
                payload["document"] = document
        payload["in_progress"] = _in_progress(store, task["id"])
        key = "notice:" + canonical_hash({"task": task["id"], "proposals": sorted(p["id"] for p in items)})[:24]
        outbox_id = str(uuid4())
        created = store.enqueue_notification(
            outbox_id=outbox_id, destination_id=DESTINATION_ID, followup_id=None, proposal_id=None,
            notification_key=key, payload=payload, now=now_iso, task_id=task["id"], kind="notice",
        )
        if not created.get("created"):  # already queued: force that row instead of duplicating
            forced = store.force_outbox(created["id"], now=now_iso)["payload"]
            task_id = task["id"]
            return {
                "kind": "queued", "task_id": task_id, "title": str(task.get("title") or ""),
                "proposal_ids": list(forced.get("proposal_ids") or []),
                "outbox_id": created["id"], "quiet": quiet, "backlog": 0,
            }
        store.mark_proposals_sent([p["id"] for p in items], outbox_id, now_iso)
        return {
            "kind": "notice", "task_id": task["id"], "title": str(task.get("title") or ""),
            "proposal_ids": [p["id"] for p in items],
            "outbox_id": outbox_id, "quiet": quiet, "backlog": 0,
        }

    decisions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    news: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for task, items in _group_by_task(store, [p for p in pending if p.get("batch_key") == "digest"]):
        for proposal in items:
            if needs_consent(config, task, proposal):
                decisions.append((task, proposal))
            elif proposal.get("operation_id") == "inform":
                news.append((task, proposal))
    if not decisions and not news:
        return {"kind": "empty"}
    sel = _order_digest(store, config, now_dt, decisions, news)
    text = fallback_text(sel["items"], language)
    payload = build_reminder({"items": sel["items"]}, text, language=language)
    payload["urgent"] = True  # the owner asked for it now: it crosses quiet hours
    listed = sel["proposal_ids"]
    key = "digest:" + canonical_hash({"at": now_iso[:13], "proposals": sorted(listed)})[:24]
    outbox_id = str(uuid4())
    created = store.enqueue_notification(
        outbox_id=outbox_id, destination_id=DESTINATION_ID, followup_id=None, proposal_id=None,
        notification_key=key, payload=payload, now=now_iso, task_id=payload.get("task_id"), kind="digest",
    )
    if not created.get("created"):  # already queued: force that row instead of duplicating
        forced = store.force_outbox(created["id"], now=now_iso)["payload"]
        task_id = payload.get("task_id")
        return {
            "kind": "queued", "task_id": task_id, "title": _task_title(store, task_id),
            "proposal_ids": list(forced.get("proposal_ids") or []),
            "outbox_id": created["id"], "quiet": quiet, "backlog": sel["backlog"],
        }
    store.mark_proposals_sent(listed, outbox_id, now_iso)
    for news_id in payload["news_ids"]:  # news told is news seen: it asks nothing and holds nothing
        try:
            store.respond_proposal(news_id, decision="declined", response={"ack": True, "auto": True, "digest": outbox_id}, now=now_iso)
        except (KeyError, ValueError, RuntimeError):
            pass
    task_id = payload.get("task_id")
    return {
        "kind": "reminder", "task_id": task_id, "title": _task_title(store, task_id),
        "proposal_ids": listed, "outbox_id": outbox_id, "quiet": quiet, "backlog": sel["backlog"],
    }


def _slot_key(proposal: dict[str, Any], config: dict[str, Any], now_dt: datetime) -> datetime:
    urgency = str((proposal.get("params") or {}).get("urgency") or "whenever")
    if urgency not in URGENCIES:
        urgency = "whenever"
    return next_delivery(now_dt, urgency, config) or (now_dt + timedelta(days=1))


def _owner_tz(config: dict[str, Any]):
    try:
        return ZoneInfo(str((config.get("owner") or {}).get("timezone") or "UTC"))
    except Exception:
        return timezone.utc


def _describe_when(language: Any, config: dict[str, Any], now_dt: datetime, when_dt: datetime | None) -> str:
    if when_dt is None:
        return t(language, "next_on_resume")
    if when_dt <= now_dt + _SOON:
        return t(language, "next_now")
    tz = _owner_tz(config)
    local_now, local_when = now_dt.astimezone(tz), when_dt.astimezone(tz)
    clock = local_when.strftime("%H:%M")
    days = (local_when.date() - local_now.date()).days
    if days <= 0:
        return t(language, "when_today", time=clock)
    if days == 1:
        return t(language, "when_tomorrow", time=clock)
    return t(language, "when_date", date=local_when.strftime("%d/%m"), time=clock)


def format_next(preview: dict[str, Any], config: dict[str, Any], now_dt: datetime) -> str:
    """The chat answer for :func:`preview_next` in the owner's language."""
    language = (config.get("owner") or {}).get("language", "es")
    kind = preview.get("kind")
    if kind == "inactive":
        if preview.get("reason") == "no_destination":
            return t(language, "next_no_destination")
        return t(language, "next_inactive", mode=preview.get("mode"))
    if kind == "empty":
        return t(language, "next_empty")
    when_dt = _parse(preview.get("when")) if preview.get("when") else None
    held = preview.get("held")
    held_quiet = _parse(preview.get("quiet_until")) if preview.get("quiet_until") else None
    when_text = _describe_when(language, config, now_dt, when_dt)
    if held == "quiet_hours" and held_quiet is not None:
        extra = t(language, "next_extra_quiet", time=held_quiet.astimezone(_owner_tz(config)).strftime("%H:%M"))
    elif held == "daily_cap":
        extra = t(language, "next_extra_cap")
    elif held == "paused":
        extra = t(language, "next_extra_paused")
        when_text = t(language, "next_on_resume")
    elif held:
        extra = f" ({held})"
    elif preview.get("urgent") and held_quiet is not None and (when_dt is None or when_dt <= now_dt + _SOON):
        extra = t(language, "next_extra_crosses")
    else:
        extra = ""
    key = {"queued": "next_queued", "notice": "next_notice", "reminder": "next_reminder"}.get(str(kind), "next_notice")
    out = t(language, key, when=when_text, extra=extra, text=preview.get("text") or "")
    if int(preview.get("backlog") or 0) > 0:
        out += t(language, "next_backlog", n=int(preview["backlog"]))
    if preview.get("task_id"):
        out += t(language, "next_id", id=str(preview["task_id"])[:8])
    return out


def format_sent(result: dict[str, Any], config: dict[str, Any]) -> str:
    """The chat answer for :func:`deliver_next`: short, since the message itself arrives next tick."""
    language = (config.get("owner") or {}).get("language", "es")
    kind = result.get("kind")
    if kind == "inactive":
        if result.get("reason") == "no_destination":
            return t(language, "next_no_destination")
        return t(language, "next_inactive", mode=result.get("mode"))
    if kind == "empty":
        return t(language, "next_empty")
    if kind == "sending":
        return t(language, "next_sending")
    extra = t(language, "next_sent_extra_quiet") if result.get("quiet") else ""
    out = t(language, "next_sent", extra=extra, title=result.get("title") or "?")
    if int(result.get("backlog") or 0) > 0:
        out += t(language, "next_backlog", n=int(result["backlog"]))
    if result.get("task_id"):
        out += t(language, "next_id", id=str(result["task_id"])[:8])
    return out

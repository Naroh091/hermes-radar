"""Turn pending proposals into outbox messages: an immediate notice per task for what cannot wait, and at each
summary slot one reminder in prose with the single most important decision (never an inventory)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
import logging

from ..i18n import t
from ..models import TASK_TERMINAL, canonical_hash
from ..trust import needs_consent
from .cadence import _tz, digest_due
from .context import thread_context
from .notice import DRAFT_INLINE, TASK_PREFIX, _esc, build_notice, decision_buttons
from .remind import fallback_text
from ..render.report import render_task_report

try:
    from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
except ImportError:  # outside the host
    def get_hermes_home() -> Path:
        return Path.home() / ".hermes"

DESTINATION_ID = "configured:primary"
log = logging.getLogger(__name__)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _deliverable(proposal: dict[str, Any], now: str) -> bool:
    after = (proposal.get("params") or {}).get("deliver_after") or ""
    return not after or after <= now


def _group_by_task(store: Any, proposals: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for proposal in proposals:
        task_id = proposal.get("task_id")
        if task_id:
            groups.setdefault(task_id, []).append(proposal)
    result = []
    for task_id, items in groups.items():
        try:
            task = store.get_task(task_id)
        except KeyError:
            continue
        if task.get("muted"):
            continue
        if task.get("status") in TASK_TERMINAL:  # settled: news may still go out, stale asks and steps never
            items = [p for p in items if p.get("operation_id") == "inform" or (p.get("params") or {}).get("owner_followup")]
            if not items:
                continue
        result.append((task, items))
    return result


def _destination_ok(store: Any) -> bool:
    try:
        store.destination(DESTINATION_ID)
        return True
    except KeyError:
        return False


def _in_progress(store: Any, task_id: str) -> list[str]:
    jobs = store.list_jobs(statuses=("queued", "running"), task_id=task_id, limit=5)
    return [str(job.get("kind") or "job") for job in jobs]


URGENCY_RANK = {"high": 0, "normal": 1, "low": 2}
_UNSET: Any = object()


def _rank_key(task: dict[str, Any], proposal: dict[str, Any]) -> tuple[int, str, str]:
    """Most urgent first, then the nearest deadline (none last), then the oldest."""
    return (URGENCY_RANK.get(str(proposal.get("urgency") or "normal"), 1), str(task.get("due_at") or "~"), str(proposal.get("created_at") or ""))


def _decision_like(proposal: dict[str, Any]) -> bool:
    operation = str(proposal.get("operation_id") or "")
    params = proposal.get("params") or {}
    return operation in ("ask", "check") or proposal.get("action_class") == "external" or (operation == "inform" and bool(params.get("ask")))


def _first_of_day(store: Any, config: dict[str, Any], now_dt: datetime) -> bool:
    last = _last_digest_at(store)
    if last is None:
        return True
    tz = _tz(config)
    return last.astimezone(tz).date() < now_dt.astimezone(tz).date()


def select_reminder(store: Any, config: dict[str, Any], now_dt: datetime) -> dict[str, Any] | None:
    """What the next reminder carries, or None when no slot is due or nothing waits: the best-placed decisions
    (``notifications.reminder_items``, one by default), at most one piece of news, and the counts the text may mention.
    The assistant's own queued work never appears: it is not the owner's to decide."""
    notifications = config.get("notifications", {})
    if config.get("mode") != "active" or not notifications.get("enabled") or not _destination_ok(store):
        return None
    now = iso(now_dt)
    pending = [p for p in store.list_proposals(statuses=("pending",), limit=200) if p.get("batch_key") == "digest" and _deliverable(p, now)]
    if not pending or not digest_due(now_dt, _last_digest_at(store), config):
        return None
    decisions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    news: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for task, items in _group_by_task(store, pending):
        for proposal in items:
            if needs_consent(config, task, proposal):
                decisions.append((task, proposal))
            elif proposal.get("operation_id") == "inform":
                news.append((task, proposal))
    if not decisions and not news:
        return None
    decisions.sort(key=lambda pair: _rank_key(*pair))
    news.sort(key=lambda pair: _rank_key(*pair))
    count = max(1, int(notifications.get("reminder_items", 1) or 1))
    chosen = decisions[:count]
    if chosen:
        chosen_news = [pair for pair in news if pair[0]["id"] == chosen[0][0]["id"]][:1]
        items = [{"task": task, "decision": proposal, "news": [n for t_, n in chosen_news if t_["id"] == task["id"]]} for task, proposal in chosen]
    else:
        chosen_news = news[:1]
        items = [{"task": chosen_news[0][0], "decision": None, "news": [chosen_news[0][1]]}]
    unanswered = [p for p in store.list_proposals(statuses=("sent",), limit=200) if _decision_like(p)]
    return {
        "items": items, "decisions": [proposal for _, proposal in chosen], "news": [proposal for _, proposal in chosen_news],
        "backlog": len(decisions) - len(chosen) + len(unanswered), "first_of_day": _first_of_day(store, config, now_dt),
    }


def build_reminder(selection: dict[str, Any], text: str, *, language: str) -> dict[str, Any]:
    """The outbox payload of a reminder: the prose, one row of buttons per decision (numbered only when there are
    several) and «later» for the task when there is exactly one."""
    decisions = [entry for entry in selection["items"] if entry.get("decision")]
    buttons: list[list[dict[str, str]]] = []
    for index, entry in enumerate(decisions, 1):
        row = decision_buttons(entry["decision"], language, index=index if len(decisions) > 1 else 0)
        if row:
            buttons.append(row)
    task_id = None
    if len(decisions) == 1:
        task_id = decisions[0]["task"]["id"]
        buttons.append([{"label": t(language, "btn_later"), "data": f"{TASK_PREFIX}:{task_id}:later"[:64]}])
    elif not decisions and selection["items"]:
        task_id = selection["items"][0]["task"]["id"]
    payload: dict[str, Any] = {
        "text": _esc(text), "parse_mode": "HTML", "buttons": buttons, "kind": "digest",
        "proposal_ids": [entry["decision"]["id"] for entry in decisions], "news_ids": [p["id"] for p in selection.get("news") or []],
    }
    if task_id:
        payload["task_id"] = task_id
    return payload


def dispatch(store: Any, config: dict[str, Any], now_dt: datetime, *, reminder: dict[str, Any] | None = _UNSET) -> dict[str, int]:
    """Enqueue notices for deliverable proposals and, when a slot is due, the reminder. ``reminder`` is the selection
    from :func:`select_reminder` with its composed ``text`` (the scheduler writes it with the model first); when it is
    not given, the selection is made here and the text comes from the template. Returns counts."""
    counts = {"notices": 0, "digests": 0, "proposals": 0}
    if config.get("mode") != "active" or not config.get("notifications", {}).get("enabled"):
        return counts
    if not _destination_ok(store):
        return counts
    now = iso(now_dt)
    language = config.get("owner", {}).get("language", "es")
    notifications = config.get("notifications", {})
    start_of_day = now_dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = store.notification_count_since(iso(start_of_day))
    limit = int(notifications.get("daily_limit", 8))
    pending = [p for p in store.list_proposals(statuses=("pending",), limit=200) if _deliverable(p, now)]
    immediate = [p for p in pending if p.get("batch_key") == "now"]
    capped = 0  # notices in this run that count against the daily cap
    for task, items in _group_by_task(store, immediate):
        exempt = any((p.get("params") or {}).get("owner_followup") for p in items)  # answers to the owner's own approvals
        if not exempt and sent_today + capped + counts["digests"] >= limit:
            continue
        message = next((p.get("params", {}).get("message") for p in items if p.get("params", {}).get("message")), "")
        payload = build_notice(task, items, language=language, message=message, context=thread_context(store, task["id"], config, now_dt=now_dt))
        payload["urgent"] = any(_urgent(p) for p in items)
        payload["exempt"] = exempt
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
            notification_key=key, payload=payload, now=now, task_id=task["id"], kind="notice",
        )
        if created.get("created"):
            store.mark_proposals_sent([p["id"] for p in items], outbox_id, now)
            counts["notices"] += 1
            capped += 0 if exempt else 1
            counts["proposals"] += len(items)
    selection = select_reminder(store, config, now_dt) if reminder is _UNSET else reminder
    if selection and sent_today + capped < limit:
        text = str(selection.get("text") or "") or fallback_text(selection["items"], language)
        payload = build_reminder(selection, text, language=language)
        listed = payload["proposal_ids"] + payload["news_ids"]
        outbox_id = str(uuid4())
        key = "digest:" + canonical_hash({"at": now[:13], "proposals": sorted(listed)})[:24]
        created = store.enqueue_notification(
            outbox_id=outbox_id, destination_id=DESTINATION_ID, followup_id=None, proposal_id=None,
            notification_key=key, payload=payload, now=now, task_id=payload.get("task_id"), kind="digest",
        )
        if created.get("created"):
            store.mark_proposals_sent(listed, outbox_id, now)
            for news_id in payload["news_ids"]:  # news told is news seen: it asks nothing and holds nothing
                try:
                    store.respond_proposal(news_id, decision="declined", response={"ack": True, "auto": True, "digest": outbox_id}, now=now)
                except (KeyError, ValueError, RuntimeError):
                    pass
            counts["digests"] += 1
            counts["proposals"] += len(listed)
    return counts


def _urgent(proposal: dict[str, Any]) -> bool:
    """A notice crosses quiet hours only for a high-urgency step the planner judged could not wait."""
    params = proposal.get("params") or {}
    return proposal.get("urgency") == "high" and str(params.get("urgency") or "now") == "now"


def draft_document(task_id: str, text: str, *, language: str, now_dt: datetime) -> dict[str, str] | None:
    """Write a drafted reply to a text file the transport can attach (Telegram messages are capped)."""
    try:
        folder = get_hermes_home() / "radar" / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"draft-{task_id[:8]}-{now_dt.strftime('%Y%m%d%H%M%S')}.txt"
        path.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return {"path": str(path), "filename": "borrador.txt" if str(language).startswith("es") else "draft.txt", "caption": t(language, "draft_file")}


def report_document(store: Any, task: dict[str, Any], *, language: str, now_dt: datetime) -> dict[str, str] | None:
    """A standalone HTML report of the task (evidence, checklist, proposals, jobs) the transport can attach."""
    try:
        html = render_task_report(task, signals=store.signals_for_task(task["id"], limit=20), proposals=store.list_proposals(task_id=task["id"], limit=20),
                                  facts=store.list_facts(limit=20), jobs=store.list_jobs(task_id=task["id"], limit=10), language=language)
        folder = get_hermes_home() / "radar" / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"task-{task['id'][:8]}-{now_dt.strftime('%Y%m%d%H%M%S')}.html"
        path.write_text(html, encoding="utf-8")
    except Exception as exc:  # a report is a courtesy, never a reason to drop the notice
        log.debug("report not written: %s", exc)
        return None
    return {"path": str(path), "filename": "informe.html" if str(language).startswith("es") else "report.html", "caption": t(language, "report_file")}


def notify_job_result(store: Any, config: dict[str, Any], *, task_id: str, signal: dict[str, Any], now_dt: datetime) -> bool:
    """Tell the owner how a step they approved ended, even when there is nothing left to decide.

    Bypasses the daily cap: the button answer promised a result.
    """
    if config.get("mode") != "active" or not config.get("notifications", {}).get("enabled") or not _destination_ok(store):
        return False
    try:
        task = store.get_task(task_id)
    except KeyError:
        return False
    language = config.get("owner", {}).get("language", "es")
    meta = signal.get("meta") or {}
    status = str(meta.get("status") or "unknown")
    step = ""
    if meta.get("proposal_id"):
        try:
            step = store.get_proposal(meta["proposal_id"]).get("title", "")
        except KeyError:
            step = ""
    headline = t(language, "approved_result_ok" if status == "succeeded" else "approved_result_unknown" if status == "unknown" else "approved_result_failed", title=step or signal.get("title", ""))
    findings = list(meta.get("findings") or [])[:5] + [f"✔ {e}" for e in (meta.get("effects") or [])[:5]] + list(meta.get("refs") or [])[:3]
    payload = build_notice(task, [], language=language, message=f"{headline}\n{signal.get('snippet') or ''}".strip(), findings=findings,
                           context=thread_context(store, task["id"], config, now_dt=now_dt))
    payload["urgent"] = status != "succeeded"
    payload["exempt"] = True
    if task.get("kind") == "work_item" and status == "succeeded" and (meta.get("findings") or meta.get("effects")):
        document = report_document(store, task, language=language, now_dt=now_dt)
        if document:
            payload["document"] = document
    payload["in_progress"] = _in_progress(store, task["id"])
    created = store.enqueue_notification(
        outbox_id=str(uuid4()), destination_id=DESTINATION_ID, followup_id=None, proposal_id=None,
        notification_key=f"jobresult:{meta.get('job_id') or signal.get('id')}", payload=payload, now=iso(now_dt),
        task_id=task["id"], kind="notice",
    )
    return bool(created.get("created"))


def _last_digest_at(store: Any) -> datetime | None:
    for row in store.list_outbox(limit=50):
        if row.get("kind") == "digest":
            try:
                return datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            except ValueError:
                return None
    return None

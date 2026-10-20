"""Turn pending proposals into outbox notices: immediate per task, digest for the rest."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
import logging

from ..i18n import t
from ..models import canonical_hash
from .cadence import digest_due
from .context import thread_context
from .notice import DRAFT_INLINE, build_digest, build_notice
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


def dispatch(store: Any, config: dict[str, Any], now_dt: datetime) -> dict[str, int]:
    """Enqueue notices for deliverable proposals. Returns counts."""
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
    digest_items = [p for p in pending if p.get("batch_key") == "digest"]
    if digest_items and sent_today + capped < limit:
        if digest_due(now_dt, _last_digest_at(store), config):
            groups = _group_by_task(store, digest_items)
            if groups:
                contexts = {task["id"]: ctx for task, _ in groups if (ctx := thread_context(store, task["id"], config, now_dt=now_dt))}
                payload = build_digest(groups, language=language, contexts=contexts)
                outbox_id = str(uuid4())
                key = "digest:" + canonical_hash({"at": now[:13], "proposals": sorted(payload["proposal_ids"])})[:24]
                created = store.enqueue_notification(
                    outbox_id=outbox_id, destination_id=DESTINATION_ID, followup_id=None, proposal_id=None,
                    notification_key=key, payload=payload, now=now, task_id=None, kind="digest",
                )
                if created.get("created"):
                    store.mark_proposals_sent(payload["proposal_ids"], outbox_id, now)
                    counts["digests"] += 1
                    counts["proposals"] += len(payload["proposal_ids"])
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

"""Decay instead of a treadmill.

A decision the owner does not answer used to live 72 hours and then be proposed again by the next re-plan, so the
same question came back three times a day. Now a decision unanswered for ``notifications.unanswered_hours`` (48)
expires as unanswered and its task is parked for a week (``snoozed``, review timers cancelled): nothing is asked
again until then, unless a new signal reopens the task, and the planner then sees what went unanswered. News that
was never delivered within a day, or stayed sent, simply stops being news.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..models import TASK_TERMINAL

log = logging.getLogger(__name__)

UNANSWERED_HOURS = 48
NEWS_HOURS = 24
PARK_DAYS = 7


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _busy(store: Any, task_id: str) -> bool:
    """A task with another open or running step, or a job in flight, is not parked: it is still moving."""
    if store.list_proposals(statuses=("pending", "sent", "snoozed", "waiting", "accepted", "executing"), task_id=task_id, limit=1):
        return True
    return bool(store.list_jobs(statuses=("queued", "running"), task_id=task_id, limit=1))


def run(store: Any, config: dict[str, Any], now_dt: datetime) -> dict[str, int]:
    notifications = config.get("notifications") or {}
    hours = float(notifications.get("unanswered_hours") or UNANSWERED_HOURS)
    now = iso(now_dt)
    result = store.decay_proposals(now, unanswered_before=iso(now_dt - timedelta(hours=hours)), news_before=iso(now_dt - timedelta(hours=NEWS_HOURS)))
    parked = 0
    for task_id in result["task_ids"]:
        try:
            task = store.get_task(task_id)
        except KeyError:
            continue
        if task.get("status") in TASK_TERMINAL or task.get("muted") or _busy(store, task_id):
            continue
        try:
            if store.park_task(task_id, until=iso(now_dt + timedelta(days=PARK_DAYS)), now=now):
                parked += 1
                store.add_audit(
                    audit_id=str(uuid4()), event_type="pipeline", subject_type="task", subject_id=task_id, correlation_id=None,
                    actor_kind="scheduler", decision_code="parked_unanswered", metadata={"until_days": PARK_DAYS, "hours": hours}, now=now,
                )
        except (KeyError, ValueError, RuntimeError) as exc:
            log.debug("task %s not parked: %s", task_id, exc)
    return {"unanswered": len(result["unanswered"]), "stale_news": len(result["stale_news"]), "parked": parked}

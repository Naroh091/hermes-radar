"""Decay instead of a treadmill: an unanswered decision expires and parks its task; stale news stops being news; queued work waits for its cap."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from conftest import NOW, FakeContext, active_config, verify_destination

from radar.jobs.runner import JobOutcome
from radar.jobs.service import record_job_outcome
from radar.pipeline.decay import run
from radar.pipeline.process import _start_auto, retry_pending_jobs, task_context
from radar.runtime import Runtime
from radar.scheduler import Scheduler

NOW_S = "2026-10-19T10:00:00Z"


def _iso(value):
    return value.astimezone().isoformat() if False else value.isoformat().replace("+00:00", "Z")


def _task(store, task_id, *, status="needs_user"):
    store.create_task(task_id=task_id, kind="work_item", title=f"Tarea {task_id}", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    if status != "open":
        store.set_task_status(task_id, status, now=NOW_S)


def _proposal(store, pid, task_id, title, *, operation="ask", action_class="read", status="pending", created=NOW_S, params=None, batch_key="digest"):
    options = [{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}] if operation == "ask" else []
    store.create_task_proposal(proposal_id=pid, task_id=task_id, title=title, reason_text="", action_class=action_class, operation_id=operation,
                               params=params or {}, options=options, urgency="normal", expires_at="2026-10-30T10:00:00Z", fingerprint=pid, now=created, batch_key=batch_key)
    if status == "sent":
        store.mark_proposals_sent([pid], "outbox-x", created)
    return store.get_proposal(pid)


def test_an_unanswered_decision_expires_and_its_task_sleeps_a_week(store):
    cfg = active_config()
    _task(store, "t1")
    _proposal(store, "q", "t1", "¿Fusionamos el PR #2041?", status="sent")
    store.upsert_followup(followup_id="task:t1:review", goal_id="tasks", goal_title="Task reviews", reason="next_step", subject_key="task:t1",
                          next_check_at="2026-10-20T10:00:00Z", expires_at="2026-11-20T10:00:00Z", assumptions={"kind": "task_review", "task_id": "t1"},
                          now=NOW_S, task_id="t1", timezone="UTC")
    assert run(store, cfg, NOW + timedelta(hours=47)) == {"unanswered": 0, "stale_news": 0, "parked": 0}
    assert store.get_proposal("q")["status"] == "sent" and store.get_task("t1")["status"] == "needs_user"
    later = NOW + timedelta(hours=49)
    assert run(store, cfg, later) == {"unanswered": 1, "stale_news": 0, "parked": 1}
    expired = store.get_proposal("q")
    assert expired["status"] == "expired" and expired["params"]["expired_reason"] == "unanswered"
    task = store.get_task("t1")
    assert task["status"] == "snoozed" and task["snoozed_until"] == _iso(later + timedelta(days=7)) and task["next_review_at"] is None
    assert store.get_followup("task:t1:review")["status"] == "cancelled"
    assert store.wake_snoozed_proposals(_iso(later)) == 0 and store.tasks_due_review(_iso(later + timedelta(days=6))) == []
    assert [t["id"] for t in store.tasks_due_review(_iso(later + timedelta(days=7, minutes=1)))] == ["t1"]  # one weekly look, not three a day
    assert task_context(store, task)["unanswered_by_owner"] == ["¿Fusionamos el PR #2041?"]  # and the planner knows it went unanswered
    assert [row for row in store.list_audit(limit=10) if row["decision_code"] == "parked_unanswered"]
    # A task that is still moving (another step open, a job running) is not parked, only its stale question expires.
    _task(store, "t2", status="in_progress")
    _proposal(store, "q2", "t2", "¿Reconstruimos dudoteca?", status="sent")
    _proposal(store, "w2", "t2", "Comprobar el despliegue", operation="investigate", batch_key="auto")
    assert run(store, cfg, later)["parked"] == 0 and store.get_proposal("q2")["status"] == "expired" and store.get_task("t2")["status"] == "in_progress"


def test_stale_news_expires_after_a_day_and_scheduled_news_waits(store):
    cfg = active_config()
    _task(store, "t1", status="in_progress")
    old = _iso(NOW - timedelta(hours=25))
    _proposal(store, "old-news", "t1", "El PR ya está verde", operation="inform", created=old)
    _proposal(store, "sent-news", "t1", "Se desplegó v11", operation="inform", created=old, status="sent")
    _proposal(store, "later-news", "t1", "Recordatorio de la gala", operation="inform", created=old, params={"deliver_after": _iso(NOW + timedelta(days=1))})
    _proposal(store, "fresh-news", "t1", "Llegó el pedido", operation="inform", created=_iso(NOW - timedelta(hours=1)))
    _proposal(store, "words", "t1", "¿Positiva o negativa?", operation="inform", created=old, params={"ask": True})
    assert run(store, cfg, NOW) == {"unanswered": 0, "stale_news": 2, "parked": 0}
    statuses = {pid: store.get_proposal(pid)["status"] for pid in ("old-news", "sent-news", "later-news", "fresh-news", "words")}
    assert statuses == {"old-news": "expired", "sent-news": "expired", "later-news": "pending", "fresh-news": "pending", "words": "pending"}
    assert store.get_proposal("old-news")["params"]["expired_reason"] == "stale_news"


def test_the_tick_runs_the_decay(store):
    cfg = active_config()
    verify_destination(store)
    _task(store, "t1")
    _proposal(store, "q", "t1", "¿Lo atacamos hoy?", status="sent")
    stats = asyncio.run(Scheduler(store, cfg, worker_id="w", clock=lambda: NOW + timedelta(hours=49)).tick())
    assert stats["decayed"] == {"unanswered": 1, "stale_news": 0, "parked": 1} and store.get_task("t1")["status"] == "snoozed"


def test_queued_work_waits_for_the_daily_cap_instead_of_asking(store, tmp_path):
    cfg = active_config(jobs={"max_auto_per_task": 1, "auto_cooldown_minutes": 0})
    _task(store, "t1", status="in_progress")
    first = _proposal(store, "a1", "t1", "Localizar el feed de Yahoo", operation="investigate", batch_key="auto")
    _proposal(store, "a2", "t1", "Comparar las fotos del feed", operation="investigate", batch_key="auto")
    rt = Runtime(FakeContext(cfg), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    assert _start_auto(rt, first, store.get_task("t1"), NOW) is True
    claimed = store.claim_queued_jobs(NOW_S, "2026-10-19T10:10:00Z", 2)[0]
    record_job_outcome(store, cfg, job=claimed, outcome=JobOutcome("succeeded", summary="ok", result={}), now_dt=NOW + timedelta(minutes=5))
    assert retry_pending_jobs(rt, NOW + timedelta(minutes=10)) == 0
    waiting = store.get_proposal("a2")
    assert waiting["status"] == "waiting" and waiting["params"]["wait_reason"] == "task_cap"  # queued, never a button
    assert retry_pending_jobs(rt, NOW + timedelta(days=1)) == 1 and store.get_proposal("a2")["status"] == "executing"

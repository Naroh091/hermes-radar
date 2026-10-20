from __future__ import annotations

import asyncio
from datetime import timedelta

from conftest import NOW, FakeContext, FakeLlm, active_config, extract_answer, plan_answer, verify_destination

from radar.pipeline import act, extract as extract_step, reason
from radar.pipeline.process import process_bundle, review_task
from radar.pipeline.sessions import session_active, task_session_active
from radar.runtime import Runtime
from radar.sources.session import SOURCE_ID

NOW_S = "2026-10-19T10:00:00Z"
SID = "20261019_234618_33f625"


def _turn(store, signal_id, at, user_text, assistant_text=""):
    store.add_signal(signal_id=signal_id, source_id=SOURCE_ID, kind="session_turn", occurred_at=at, external_ref=signal_id, group_key=f"session:{SID}",
                     title=user_text[:80], snippet=user_text, meta={"session_id": SID, "platform": "desktop", "user_text": user_text, "assistant_text": assistant_text},
                     fingerprint=signal_id, now=at)


def test_active_session_bundles_wait_and_their_tasks_stay_silent_without_parallel_jobs(store, tmp_path):
    # session_auto_jobs lets this test exercise the live-session guard alone; by default no job runs for a chat-born task.
    cfg = active_config(trust={"level": "balanced", "overrides": {}, "auto_external": []}, observation={"session_auto_jobs": True})
    verify_destination(store)
    store.ensure_local_observation_source(NOW_S, 30)
    _turn(store, "turn1", "2026-10-19T09:55:00Z", "ok, móntalo en la mariadb que hay nativa y vigila el dump de The Hub", "Parte 1 lista. Parte 2 en marcha: el dump va por 5,8 GB.")
    assert session_active(store, SID, NOW, cfg) is True and session_active(store, SID, NOW + timedelta(minutes=31), cfg) is False
    plan = plan_answer(
        task={"kind": "work_item", "title": "Restaurar dms_maldita y montar The Hub en local", "summary": "El dump corre en el host remoto.", "status": "in_progress", "due_at": "", "next_review_at": "", "confidence": 0.8},
        checklist=[], proposals=[{"title": "Vigilar el dump de The Hub", "rationale": "", "action_class": "read", "operation": "investigate", "when": "", "urgency": "normal", "options": [{"id": "do", "label": "Sí"}]}],
        notify={"disposition": "notify_now", "reason": "x"}, message="Hay un dump en marcha.", escalation={"decision": "handle", "why": "x"},
    )
    llm = FakeLlm({"radar.extract": [extract_answer(category="work_item", proposed_reply="Hola, he vigilado el dump…", conversation_state="open")], "radar.plan": [plan, plan]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    bundle = store.claim_signal_bundles(NOW_S, "2026-10-19T10:10:00Z", 5)[0]
    # While the conversation is live nothing is processed: the bundle waits for the quiet window.
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome["deferred"] == "session_active" and llm.calls == []
    signal = store.get_signal("turn1")
    assert signal["triage_status"] == "pending" and signal["next_attempt_at"] == "2026-10-19T10:30:00Z" and signal["triage_reason"] == "session_active"
    # Once quiet, the task is created and remembers its session; a reply draft is dropped (nobody to answer).
    later = NOW + timedelta(minutes=31)
    rt.clock = lambda: later
    bundle = store.claim_signal_bundles("2026-10-19T10:31:00Z", "2026-10-19T10:41:00Z", 5)[0]
    outcome = asyncio.run(process_bundle(rt, bundle, later))
    task = store.get_task(outcome["task_id"])
    assert task["entities"]["session_id"] == SID and "draft" not in task["entities"]
    assert outcome["auto_jobs"] == 1  # the session is over: the read step may run now
    # The owner comes back to the chat: the task falls silent again, reviews wait, no new jobs.
    _turn(store, "turn2", "2026-10-19T10:40:00Z", "como va", "Sigue en marcha: 6,5 GB.")
    now2 = later + timedelta(minutes=10)
    assert task_session_active(store, task, now2, cfg) is True
    review = asyncio.run(review_task(rt, task, now2))
    assert review["skipped"] == "session_active" and store.get_task(task["id"])["next_review_at"] == "2026-10-19T11:11:00Z"
    from radar.pipeline.process import _auto_eligible
    proposal = {"operation_id": "investigate", "action_class": "read"}
    rt.clock = lambda: now2
    assert _auto_eligible(rt, proposal, store.get_task(task["id"])) is False
    rt.clock = lambda: now2 + timedelta(minutes=40)
    assert _auto_eligible(rt, proposal, store.get_task(task["id"])) is True
    assert "A conversation bundle is a chat the owner had with their agent" in reason.PLAN_PROMPT


def test_apply_silences_notices_for_tasks_whose_session_is_live(store):
    cfg = active_config()
    store.ensure_local_observation_source(NOW_S, 30)
    _turn(store, "t1", "2026-10-19T09:58:00Z", "busca RAM DDR5 32 GB")
    signals = [store.get_signal("t1")]
    plan = reason.validate_plan(plan_answer(task={"kind": "purchase", "title": "RAM DDR5 32 GB", "summary": "", "status": "open", "due_at": "", "next_review_at": "", "confidence": 0.7},
                                            checklist=[], notify={"disposition": "notify_now", "reason": "x"}, message="Buscando RAM."))
    result = act.apply(store, cfg, signals=signals, extract=extract_step.validate_extract(extract_answer(category="purchase")), plan=plan, now_dt=NOW, preferences=[])
    assert result.disposition == "silent" and store.get_task(result.task_id)["entities"]["session_id"] == SID
    proposals = store.list_proposals(task_id=result.task_id)
    assert all(p["batch_key"] == "silent" for p in proposals if not p["params"].get("when"))
    assert all(p["batch_key"] == "now" for p in proposals if p["params"].get("when"))  # a step for a later moment speaks then, once the chat is over

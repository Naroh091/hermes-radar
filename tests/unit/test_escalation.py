from __future__ import annotations

import asyncio
import json

from conftest import NOW, FakeContext, FakeLlm, active_config, extract_answer, plan_answer, verify_destination

from radar.delivery.dispatch import dispatch
from radar.delivery.notice import build_notice
from radar.delivery.responses import respond_to_proposal
from radar.jobs.service import enqueue_job_for_proposal, job_block_reason
from radar.operations import native
from radar.pipeline import reason
from radar.pipeline.process import process_bundle, task_context
from radar.runtime import Runtime
from radar.trust import may_auto_run

NOW_S = "2026-10-19T10:00:00Z"


def _email(store, signal_id="s1", title="Dos pagos en proceso en Payoneer", sender="Payoneer <no-reply@payoneer.com>"):
    store.add_signal(signal_id=signal_id, source_id="src", kind="email", occurred_at=NOW_S, external_ref=f"m-{signal_id}", group_key=f"g-{signal_id}",
                     title=title, snippet="Tienes dos pagos en proceso.", meta={"sender": sender, "account": "me@example.org"}, fingerprint=f"f-{signal_id}", now=NOW_S)


def test_validate_plan_defaults_escalation_to_ask():
    plan = reason.validate_plan(plan_answer())
    assert plan["escalation"] == {"decision": "ask", "why": ""}
    plan = reason.validate_plan(plan_answer(escalation={"decision": "handle", "why": "the assistant can check the payments itself"}))
    assert plan["escalation"]["decision"] == "handle"
    assert reason.validate_plan(plan_answer(escalation={"decision": "bogus"}))["escalation"]["decision"] == "ask"
    assert "escalation" in reason.PLAN_PROMPT and "task.update" in reason.PLAN_PROMPT


def test_handle_investigates_silently_first_then_inform_tells_the_news_without_asking(store, tmp_path):
    cfg = active_config(trust={"level": "balanced", "overrides": {}, "auto_external": []})
    verify_destination(store)
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=NOW_S)
    _email(store)
    handle = plan_answer(
        task={"kind": "commitment", "title": "Cobro de dos pagos de Payoneer", "summary": "Payoneer anuncia dos pagos en proceso.", "status": "open", "due_at": "", "next_review_at": "", "confidence": 0.8},
        checklist=[], proposals=[{"title": "Comprobar en Payoneer si los pagos han llegado y de qué facturas son", "rationale": "", "action_class": "read", "operation": "investigate",
                                  "when": "", "urgency": "normal", "options": [{"id": "do", "label": "Sí"}]}],
        notify={"disposition": "notify_now", "reason": "money"}, message="Payoneer dice que hay dos pagos en proceso.",
        escalation={"decision": "handle", "why": "the assistant can check the account"},
    )
    rt = Runtime(FakeContext(cfg, llm=FakeLlm({"radar.extract": [extract_answer(category="commitment")], "radar.plan": [handle]})), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    bundle = store.claim_signal_bundles(NOW_S, "2026-10-19T10:10:00Z", 5)[0]
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome["disposition"] == "silent" and outcome["auto_jobs"] == 1  # nothing sent; the read step runs by itself
    task_id = outcome["task_id"]
    proposal = store.list_proposals(task_id=task_id)[0]
    assert proposal["batch_key"] == "auto" and proposal["status"] == "executing"  # the assistant's own work: never a button
    assert dispatch(store, cfg, NOW) == {"notices": 0, "digests": 0, "proposals": 0}
    # The investigation result comes back: the planner now informs (good news) and offers to close the invoice task.
    store.create_task(task_id="inv", kind="commitment", title="Factura 2026-031 pendiente de cobro", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    inform = plan_answer(
        task_match={"decision": "advances", "task_id": task_id, "why": "result"},
        task={"kind": "commitment", "title": "Cobro de dos pagos de Payoneer", "summary": "Los dos pagos han llegado: 1.200 € y 300 €.", "status": "done", "due_at": "", "next_review_at": "", "confidence": 0.9},
        checklist=[], proposals=[], notify={"disposition": "notify_now", "reason": "money received"},
        message="Enhorabuena, has cobrado 1.500 € de Payoneer 🤑 (facturas 2026-031 y 2026-032).",
        escalation={"decision": "inform", "why": "nothing to decide"},
    )
    rt.ctx.llm = FakeLlm({"radar.plan": [inform]})
    from radar.jobs.runner import JobOutcome
    from radar.jobs.service import record_job_outcome
    job = store.claim_queued_jobs(NOW_S, "2026-10-19T10:10:00Z", 5)[0]
    record_job_outcome(store, cfg, job=job, outcome=JobOutcome("succeeded", summary="Both payments arrived", result={"findings": ["1.200 € invoice 2026-031", "300 € invoice 2026-032"]}), now_dt=NOW)
    result_bundle = store.claim_signal_bundles(NOW_S, "2026-10-19T10:10:00Z", 5)[0]
    outcome2 = asyncio.run(process_bundle(rt, result_bundle, NOW))
    assert outcome2["disposition"] == "notify_now"
    news = [p for p in store.list_proposals(task_id=task_id, limit=10) if p.get("operation_id") == "inform"]
    assert len(news) == 1 and news[0]["options"] == [{"id": "ok", "label": "👍 Vale"}] and news[0]["title"].startswith("Enhorabuena")
    counts = dispatch(store, cfg, NOW)
    assert counts["notices"] == 1
    text = store.list_outbox(limit=1)[0]["payload"]["text"]
    assert "has cobrado 1.500" in text and "<b>Puedo hacer:</b>" not in text and "1." not in text.replace("1.500", "")
    assert store.list_outbox(limit=1)[0]["payload"]["buttons"][0][0]["label"] == "👍 Vale"
    # "Vale" acknowledges: no job, and it is never remembered as something the owner refused.
    assert job_block_reason(store, cfg, proposal=news[0], now_dt=NOW) == "check"
    ack = respond_to_proposal(store, cfg, proposal_id=news[0]["id"], option="ok", actor="telegram:7", now_dt=NOW)
    assert ack["outcome"] == "acknowledged" and store.get_proposal(news[0]["id"])["status"] == "declined"
    assert task_context(store, store.get_task(task_id))["declined_by_owner"] == []


def test_task_update_offer_runs_natively_only_after_the_owner_says_so(store, tmp_path):
    cfg = active_config()
    verify_destination(store)
    store.create_task(task_id="inv", kind="commitment", title="Factura 2026-031 pendiente de cobro", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    store.create_task_proposal(proposal_id="p1", task_id="inv", title="Marcar la factura 2026-031 como cobrada", reason_text="Payoneer la ha pagado", action_class="local", operation_id="task.update", params={},
                               options=[{"id": "mark_collected", "label": "Marcar como cobrada"}, {"id": "no", "label": "No"}], urgency="low", expires_at="2026-10-22T10:00:00Z", fingerprint="fp1", now=NOW_S)
    assert may_auto_run({"level": "autonomous"}, "local", operation_id="task.update") is False
    store.respond_proposal("p1", decision="accepted", response={"option": "mark_collected", "actor": "telegram:7", "channel": "telegram"}, now=NOW_S)
    plan = native.plan(store, store.get_proposal("p1"), store.get_task("inv"), "mark_collected")
    assert plan == {"operation": "task.update", "params": {"task_id": "inv", "status": "done", "note": "Marcar la factura 2026-031 como cobrada"}}
    rt = Runtime(FakeContext(cfg, llm=None), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    job = enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("p1"), option="mark_collected", now_dt=NOW)
    assert job["kind"] == "native" and json.loads(job["prompt"])["params"]["status"] == "done"
    claimed = store.claim_queued_jobs(NOW_S, "2026-10-19T10:10:00Z", 5)
    outcome = asyncio.run(native.run_job(rt, claimed[0]))
    assert outcome.status == "succeeded" and store.get_task("inv")["status"] == "done"

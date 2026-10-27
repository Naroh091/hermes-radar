from __future__ import annotations

import asyncio
from datetime import timedelta

from conftest import NOW, FakeContext, FakeLlm, active_config, config, extract_answer, plan_answer

from radar.llm import LlmError
from radar.pipeline import act, extract as extract_step, reason
from radar.pipeline.process import process_bundle, review_task
from radar.pipeline.triage import triage_signal
from radar.runtime import Runtime


def _email(**meta):
    base = {"sender": "noreply@tap.com", "account": "a@example.org", "automation": ["noreply-sender"]}
    base.update(meta)
    return {"id": "s1", "kind": "email", "title": "Confirmación de reserva ABC123", "snippet": "Vuelo Madrid-Lisboa 3 nov", "meta": base, "attempts": 1}


def test_triage_rules_keep_transactional_automated_mail_and_drop_bulk():
    assert triage_signal(_email(), []).keep
    newsletter = _email(automation=["list-unsubscribe", "noreply-sender"])
    newsletter["title"] = "Newsletter semanal: novedades"
    newsletter["snippet"] = "ofertas"
    assert triage_signal(newsletter, []).reason == "noise_subject"
    bulk = _email(automation=["list-unsubscribe"])
    bulk["title"] = "Hola"
    bulk["snippet"] = "nada"
    assert triage_signal(bulk, []).reason == "bulk_no_signal"
    pref = [{"id": "p", "status": "active", "dimension": "notification", "scope": {"sender": "noreply@tap.com"}, "effect": "silent", "exceptions": [], "origin": "explicit"}]
    assert triage_signal(_email(), pref).reason == "preference:p"
    turn = {"id": "s2", "kind": "session_turn", "title": "x", "snippet": "/help", "meta": {"user_text": "/help"}}
    assert triage_signal(turn, []).reason == "command"
    turn["meta"]["user_text"] = "busca RAM DDR5 de 32 GB para el portátil"
    assert triage_signal(turn, []).priority == "normal"


def test_triage_drops_routine_portal_access_notices():
    # The sede electrónica's standard "you signed in" mail: transactional-looking (certificate), nothing to do.
    sede = _email(automation=[])
    sede["title"] = "Sesión iniciada desde un nuevo navegador web"
    sede["snippet"] = ("Se ha iniciado sesión desde un nuevo navegador web en la sede electrónica usando el "
                       "certificado digital de persona física de DAVID ALEJANDRO FERNANDEZ SANCHO.")
    assert triage_signal(sede, []).keep is False and triage_signal(sede, []).reason == "access_notification"
    newsignin = _email(automation=[])
    newsignin["title"] = "New sign-in to your account"
    newsignin["snippet"] = "We noticed a new sign-in from Chrome on Mac."
    assert triage_signal(newsignin, []).keep is False
    # Official administrative e-notifications: a notice waiting in a sede, the owner collects it himself.
    enotif = _email(automation=[])
    enotif["title"] = "Nueva notificación electrónica para DAVID ALEJANDRO FERNANDEZ SANCHO"
    enotif["snippet"] = "Tiene una nueva notificación electrónica dirigida a usted procedente del Consejo de Transparencia."
    assert triage_signal(enotif, []).keep is False and triage_signal(enotif, []).reason == "e_notification"
    platform = _email(automation=[])
    platform["title"] = "Notificación enviada"
    platform["snippet"] = "<table>...</table>"
    assert triage_signal(platform, []).keep is False and triage_signal(platform, []).reason == "e_notification"
    # A real thread about requesting access is not a login notice and still gets through.
    request = _email(automation=[])
    request["title"] = "Necesitamos acceso al backend EFCSN"
    request["snippet"] = "¿Nos pasas la URL y un usuario de solo lectura?"
    assert triage_signal(request, []).keep is True


def test_extract_and_plan_validators_reject_out_of_range_values():
    parsed = extract_step.validate_extract(extract_answer())
    assert parsed["category"] == "trip" and parsed["entities"]["places"] == ["Lisboa"] and parsed["facts_learned"][0]["value"] == "42"
    try:
        extract_step.validate_extract(extract_answer(disposition="loud"))
        assert False, "expected LlmError"
    except LlmError:
        pass
    plan = reason.validate_plan(plan_answer())
    assert plan["task_match"]["decision"] == "new" and len(plan["proposals"]) == 2 and plan["proposals"][1]["when"].startswith("2026-11-02")
    try:
        reason.validate_plan({"task_match": {"decision": "maybe"}})
        assert False, "expected LlmError"
    except LlmError:
        pass


def test_apply_creates_task_proposals_facts_and_review_timer(store):
    cfg = config()
    signals = [{"id": "s1", "kind": "email", "title": "Reserva", "meta": {"sender": "noreply@tap.com", "account": "a@example.org"}}]
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now="2026-10-19T10:00:00Z")
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at="2026-10-19T10:00:00Z", external_ref="m1", group_key="g",
                     title="Reserva", snippet="", meta=signals[0]["meta"], fingerprint="f", now="2026-10-19T10:00:00Z")
    result = act.apply(store, cfg, signals=signals, extract=extract_step.validate_extract(extract_answer()),
                       plan=reason.validate_plan(plan_answer()), now_dt=NOW, preferences=[])
    task = store.get_task(result.task_id)
    assert result.created and task["kind"] == "trip" and task["status"] == "open" and task["due_at"] == "2026-11-03T06:00:00Z"
    assert [c["status"] for c in task["checklist"][:2]] == ["done", "missing"]
    assert store.list_facts()[0]["key"] == "shoe size"
    proposals = store.list_proposals(task_id=task["id"])
    assert len(proposals) == 2 and {p["batch_key"] for p in proposals} == {"auto"}  # the assistant's own work: it runs by itself
    scheduled = next(p for p in proposals if p["title"].startswith("Hacer check-in"))
    assert scheduled["params"]["deliver_after"] == "2026-11-02T06:00:00Z"
    followup = store.get_followup(f"task:{task['id']}:review")
    assert followup["next_check_at"] == "2026-11-02T06:00:00Z" and followup["task_id"] == task["id"]
    assert store.signals_for_task(task["id"])[0]["role"] == "origin"
    # A completing bundle closes the task and cancels its timer.
    done = plan_answer(task_match={"decision": "completes", "task_id": task["id"], "why": "hotel booked"},
                       task={**plan_answer()["task"], "status": "done"}, proposals=[])
    second = act.apply(store, cfg, signals=[], extract=extract_step.validate_extract(extract_answer(category="trip")),
                       plan=reason.validate_plan(done), now_dt=NOW + timedelta(days=1), preferences=[])
    assert second.task_id == task["id"] and store.get_task(task["id"])["status"] == "done"
    assert store.get_followup(f"task:{task['id']}:review")["status"] == "cancelled"


def _runtime(store, cfg, llm):
    ctx = FakeContext(cfg, llm=llm)
    return Runtime(ctx, store, cfg, worker_id="w", clock=lambda: NOW)


def test_process_bundle_end_to_end_and_auto_runs_local_steps(store):
    cfg = active_config(trust={"level": "balanced", "overrides": {}, "auto_external": []})
    plan = plan_answer(proposals=[{"title": "Buscar hoteles", "rationale": "", "action_class": "read", "operation": "web.read", "when": "",
                                   "urgency": "normal", "options": [{"id": "do", "label": "Sí"}]}])
    rt = _runtime(store, cfg, FakeLlm({"radar.extract": [extract_answer()], "radar.plan": [plan]}))
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now="2026-10-19T10:00:00Z")
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at="2026-10-19T10:00:00Z", external_ref="m1", group_key="g",
                     title="Confirmación de reserva", snippet="vuelo", meta={"sender": "noreply@tap.com", "account": "a@example.org", "automation": ["noreply-sender"]},
                     fingerprint="f", now="2026-10-19T10:00:00Z", raw_content="cuerpo completo", raw_expires_at="2026-10-21T10:00:00Z")
    bundles = store.claim_signal_bundles("2026-10-19T10:00:00Z", "2026-10-19T10:10:00Z", 5)
    outcome = asyncio.run(process_bundle(rt, bundles[0], NOW))
    assert outcome["decision"] == "new" and outcome["disposition"] == "notify_now" and outcome["auto_jobs"] == 1
    signal = store.get_signal("s1")
    assert signal["triage_status"] == "extracted" and signal["raw_content"] is None
    jobs = store.list_jobs()
    assert len(jobs) == 1 and jobs[0]["status"] == "queued" and "Buscar hoteles" in jobs[0]["prompt"]
    proposal = store.list_proposals(task_id=outcome["task_id"])[0]
    assert proposal["status"] == "executing" and proposal["response"]["auto"] is True
    assert store.budget_usage("2026-10-19", "llm") == {"extract": {"reserved": 0, "consumed": 1}, "plan": {"reserved": 0, "consumed": 1}}


def test_process_bundle_defers_on_budget_and_marks_noise(store):
    cfg = config(limits={"llm_calls_per_day": 1})
    rt = _runtime(store, cfg, FakeLlm({"radar.extract": [extract_answer(relevant=False, category="noise")]}))
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now="2026-10-19T10:00:00Z")
    for ref in ("m1", "m2"):
        store.add_signal(signal_id=f"s-{ref}", source_id="src", kind="email", occurred_at="2026-10-19T10:00:00Z", external_ref=ref, group_key=ref,
                         title="Pedido enviado", snippet="x", meta={"sender": "shop@example.org"}, fingerprint=ref, now="2026-10-19T10:00:00Z")
    bundles = store.claim_signal_bundles("2026-10-19T10:00:00Z", "2026-10-19T10:10:00Z", 5)
    first = asyncio.run(process_bundle(rt, bundles[0], NOW))
    second = asyncio.run(process_bundle(rt, bundles[1], NOW))
    assert first["result"] == "noise" and store.get_signal("s-m1")["triage_status"] == "ignored"
    assert second["deferred"] == "budget" and store.get_signal("s-m2")["next_attempt_at"] == "2026-10-19T16:00:00Z"
    assert store.list_audit(subject_id=None)[0]["decision_code"] in {"budget_exhausted", "completed"}


def test_review_task_replans_without_new_signals(store):
    cfg = config()
    store.create_task(task_id="t1", kind="trip", title="Lisboa", summary="falta hotel", entities={}, expectations=[], checklist=[], now="2026-10-19T10:00:00Z")
    plan = plan_answer(task_match={"decision": "advances", "task_id": "t1", "why": "review"},
                       proposals=[{"title": "Hacer check-in online", "rationale": "ya se puede", "action_class": "local", "operation": "", "when": "",
                                   "urgency": "high", "options": []}], notify={"disposition": "notify_now", "reason": "check-in"})
    rt = _runtime(store, cfg, FakeLlm({"radar.plan": [plan]}))
    outcome = asyncio.run(review_task(rt, store.get_task("t1"), NOW))
    assert outcome["proposals"] == 1 and outcome["auto_jobs"] == 1
    assert store.get_task("t1")["status"] == "in_progress" and store.get_task("t1")["version"] >= 2
    assert store.list_proposals(task_id="t1")[0]["urgency"] == "high"


def test_runtime_refresh_config_applies_edits_and_rematerialises_sources(store):
    cfg = config()
    ctx = FakeContext(cfg, llm=None)
    rt = Runtime(ctx, store, cfg, worker_id="w", clock=lambda: NOW)
    assert rt.refresh_config() is False
    ctx.cfg = {**cfg, "trust": {"level": "cautious", "overrides": {}, "auto_external": []},
               "sources": [{"id": "gmail-a", "kind": "gmail", "address": "a@example.org", "server": "gmail-a"}]}
    assert rt.refresh_config() is True
    assert rt.config["trust"]["level"] == "cautious" and rt.scheduler.config is rt.config
    assert store.get_source("configured:gmail-a")["kind"] == "gmail"
    assert rt.refresh_config() is False


def test_auto_run_defers_the_second_step_and_retries_when_the_job_ends(store):
    from types import SimpleNamespace

    from conftest import verify_destination

    from radar.delivery.dispatch import dispatch
    from radar.jobs.runner import JobOutcome
    from radar.jobs.service import record_job_outcome
    from radar.pipeline.process import retry_pending_jobs

    cfg = active_config(trust={"level": "balanced", "overrides": {}, "auto_external": []})
    steps = [
        {"title": "Buscar hoteles", "rationale": "", "action_class": "read", "operation": "web.read", "when": "", "urgency": "normal", "options": [{"id": "do", "label": "Sí"}]},
        {"title": "Comparar precios de vuelta", "rationale": "", "action_class": "read", "operation": "web.read", "when": "", "urgency": "normal", "options": [{"id": "do", "label": "Sí"}]},
    ]
    rt = _runtime(store, cfg, FakeLlm({"radar.extract": [extract_answer()], "radar.plan": [plan_answer(proposals=steps)]}))
    verify_destination(store)
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now="2026-10-19T10:00:00Z")
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at="2026-10-19T10:00:00Z", external_ref="m1", group_key="g",
                     title="Confirmación de reserva", snippet="vuelo", meta={"sender": "noreply@tap.com", "account": "a@example.org", "automation": ["noreply-sender"]},
                     fingerprint="f", now="2026-10-19T10:00:00Z")
    bundle = store.claim_signal_bundles("2026-10-19T10:00:00Z", "2026-10-19T10:10:00Z", 5)[0]
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome["auto_jobs"] == 1
    proposals = store.list_proposals(task_id=outcome["task_id"])
    first = next(p for p in proposals if p["title"] == "Buscar hoteles")
    second = next(p for p in proposals if p["title"] == "Comparar precios de vuelta")
    assert first["status"] == "executing"
    assert second["status"] == "waiting" and second["params"]["wait_reason"] == "active_job" and second["response"]["auto"] is True
    assert dispatch(store, cfg, NOW)["notices"] == 0  # waiting steps are not turned into questions
    claimed = store.claim_queued_jobs("2026-10-19T10:00:00Z", "2026-10-19T10:10:00Z", 2)[0]
    record_job_outcome(store, cfg, job=claimed, outcome=JobOutcome("succeeded", summary="ok", result={}), now_dt=NOW)
    assert retry_pending_jobs(rt, NOW + timedelta(minutes=10)) == 0  # cooldown between automatic jobs
    parked = store.get_proposal(second["id"])
    assert parked["status"] == "waiting" and parked["params"]["wait_reason"] == "cooldown"
    assert retry_pending_jobs(rt, NOW + timedelta(minutes=20)) == 1
    assert store.get_proposal(second["id"])["status"] == "executing" and len(store.list_jobs()) == 2
    # Lowering trust hands a waiting automatic step back to the owner instead of running it.
    store.create_task_proposal(proposal_id="p-local", task_id=outcome["task_id"], title="Preparar borrador", reason_text="", action_class="local",
                               operation_id="gmail.create_draft", params={}, options=[], urgency="normal",
                               expires_at="2026-10-22T10:00:00Z", fingerprint="fpl", now="2026-10-19T10:00:00Z", batch_key="now")
    store.respond_proposal("p-local", decision="accepted", response={"auto": True}, now="2026-10-19T10:00:00Z")
    store.set_proposal_status("p-local", "waiting", "2026-10-19T10:00:00Z")
    rt.config["trust"]["level"] = "cautious"
    assert retry_pending_jobs(rt, NOW + timedelta(minutes=30)) == 0
    assert store.get_proposal("p-local")["status"] == "pending" and dispatch(store, cfg, NOW + timedelta(minutes=30))["notices"] == 1


def test_retry_starts_owner_approved_steps_when_budget_frees(store):
    from types import SimpleNamespace

    from radar.jobs.runner import JobOutcome
    from radar.jobs.service import enqueue_job_for_proposal, record_job_outcome, start_or_wait
    from radar.pipeline.process import retry_pending_jobs

    cfg = config(limits={"jobs_per_day": 1})
    rt = SimpleNamespace(store=store, config=cfg, clock=lambda: NOW)
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="DNS", summary="", entities={}, expectations=[], checklist=[], now=now)
    for pid, klass in (("p1", "read"), ("p2", "external")):
        store.create_task_proposal(proposal_id=pid, task_id="t1", title=f"Paso {pid}", reason_text="", action_class=klass, operation_id="investigate" if klass == "read" else None,
                                   params={}, options=[{"id": "do", "label": "Sí"}], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint=pid, now=now)
    store.respond_proposal("p1", decision="accepted", response={"option": "do"}, now=now)
    assert enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("p1"), option="do", now_dt=NOW)
    claimed = store.claim_queued_jobs(now, "2026-10-19T10:10:00Z", 2)[0]
    record_job_outcome(store, cfg, job=claimed, outcome=JobOutcome("succeeded", summary="ok", result={}), now_dt=NOW)
    store.respond_proposal("p2", decision="accepted", response={"option": "do", "actor": "telegram:7", "channel": "telegram"}, now=now)
    job, reason = start_or_wait(store, cfg, proposal=store.get_proposal("p2"), option="do", now_dt=NOW)
    assert job is None and reason == "budget" and store.get_proposal("p2")["status"] == "waiting"
    assert retry_pending_jobs(rt, NOW) == 0 and store.get_proposal("p2")["status"] == "waiting"
    assert retry_pending_jobs(rt, NOW + timedelta(days=1)) == 1
    assert store.get_proposal("p2")["status"] == "executing" and "OWNER APPROVAL" in store.list_jobs()[0]["prompt"]
    # An accepted step that never got a job is parked as waiting too, never left dangling.
    store.create_task_proposal(proposal_id="p3", task_id="t1", title="Paso p3", reason_text="", action_class="read", operation_id="investigate",
                               params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="p3", now=now)
    store.respond_proposal("p3", decision="accepted", response={"option": "do"}, now=now)
    assert retry_pending_jobs(rt, NOW + timedelta(days=1, minutes=1)) == 0
    assert store.get_proposal("p3")["status"] == "waiting" and store.get_proposal("p3")["params"]["wait_reason"] == "active_job"


def test_apply_skips_steps_the_owner_declined_and_tells_the_planner(store):
    from radar.delivery.responses import respond_to_proposal
    from radar.pipeline.process import task_context

    cfg = config()
    signals = [{"id": "s1", "kind": "email", "title": "Reserva", "meta": {"sender": "noreply@tap.com", "account": "a@example.org"}}]
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now="2026-10-19T10:00:00Z")
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at="2026-10-19T10:00:00Z", external_ref="m1", group_key="g",
                     title="Reserva", snippet="", meta=signals[0]["meta"], fingerprint="f", now="2026-10-19T10:00:00Z")
    result = act.apply(store, cfg, signals=signals, extract=extract_step.validate_extract(extract_answer()),
                       plan=reason.validate_plan(plan_answer()), now_dt=NOW, preferences=[])
    task = store.get_task(result.task_id)
    hotel = next(p for p in store.list_proposals(task_id=task["id"]) if p["title"].startswith("Buscar hotel"))
    respond_to_proposal(store, cfg, proposal_id=hotel["id"], option="no", actor="telegram:7", now_dt=NOW)
    assert task_context(store, task)["declined_by_owner"] == [hotel["title"]]
    replan = plan_answer(task_match={"decision": "advances", "task_id": task["id"], "why": "review"}, proposals=[
        {"title": "Comparar hoteles cerca del centro", "rationale": "", "action_class": "read", "operation": "web.read", "when": "", "urgency": "normal", "options": []},
        {"title": "Preparar borrador para la agencia", "rationale": "", "action_class": "local", "operation": "gmail.create_draft", "when": "", "urgency": "normal", "options": []},
    ])
    act.apply(store, cfg, signals=[], extract=None, plan=reason.validate_plan(replan), now_dt=NOW + timedelta(hours=1), preferences=[], existing_task=task)
    pending = [p["title"] for p in store.list_proposals(statuses=("pending",), task_id=task["id"])]
    assert "Comparar hoteles cerca del centro" not in pending and "Preparar borrador para la agencia" in pending
    audit = [row for row in store.list_audit(limit=20) if row["decision_code"] == "advances"]
    assert audit and audit[-1]["metadata_redacted"]["suppressed"] == 1


def test_owner_approved_job_result_always_reaches_the_owner(store):
    from conftest import verify_destination

    from radar.jobs.runner import JobOutcome
    from radar.jobs.service import enqueue_job_for_proposal, record_job_outcome

    cfg = active_config(notifications={"daily_limit": 1})
    verify_destination(store)
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Apuntar DNS", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Autorizar la creación del CNAME", reason_text="", action_class="external", operation_id=None,
                               params={}, options=[{"id": "ok", "label": "Sí"}], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fp1", now=now)
    store.respond_proposal("p1", decision="accepted", response={"option": "ok", "actor": "telegram:7", "channel": "telegram"}, now=now)
    assert enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("p1"), option="ok", now_dt=NOW)
    claimed = store.claim_queued_jobs(now, "2026-10-19T10:10:00Z", 2)[0]
    record_job_outcome(store, cfg, job=claimed, outcome=JobOutcome("succeeded", summary="CNAME creado y verificado.", result={"effects": ["CNAME red-electoral creado"], "findings": ["resuelve ya"]}), now_dt=NOW)
    # The planner finds nothing left to decide: digest, no proposals. The owner still gets the result.
    plan = plan_answer(task_match={"decision": "advances", "task_id": "t1", "why": "job"}, proposals=[], notify={"disposition": "digest", "reason": "done"})
    rt = _runtime(store, cfg, FakeLlm({"radar.plan": [plan]}))
    bundle = store.claim_signal_bundles(now, "2026-10-19T10:10:00Z", 5)[0]
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome["result_notice"] is True
    notice = store.list_outbox(limit=5)[0]
    assert notice["kind"] == "notice" and "Resultado del paso que autorizaste" in notice["payload"]["text"] and "CNAME creado y verificado" in notice["payload"]["text"]
    assert "✔ CNAME red-electoral creado" in notice["payload"]["text"]


def test_drafted_replies_stay_on_the_task_and_show_in_the_notice(store):
    from radar.delivery.notice import build_notice

    cfg = config()
    signals = [{"id": "s1", "kind": "email", "title": "Bug", "meta": {"sender": "bernd@example.org", "account": "a@example.org"}}]
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now="2026-10-19T10:00:00Z")
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at="2026-10-19T10:00:00Z", external_ref="m1", group_key="g",
                     title="Bug", snippet="", meta=signals[0]["meta"], fingerprint="f", now="2026-10-19T10:00:00Z")
    extract = extract_step.validate_extract(extract_answer(category="work_item", proposed_reply="Hi Bernd,\n\nCould you send screenshots?"))
    extract["job"] = {"refs": ["https://x", "Draft ID: r123"], "status": "succeeded"}
    plan = plan_answer(task={**plan_answer()["task"], "kind": "work_item"}, proposals=[
        {"title": "Enviar la solicitud a Bernd", "rationale": "El borrador está listo", "action_class": "external", "operation": "gmail.reply",
         "when": "", "urgency": "normal", "options": [{"id": "do_it", "label": "Enviar borrador"}, {"id": "review_first", "label": "Revisarlo primero"}]},
    ])
    result = act.apply(store, cfg, signals=signals, extract=extract, plan=reason.validate_plan(plan), now_dt=NOW, preferences=[])
    task = store.get_task(result.task_id)
    assert task["entities"]["draft"]["text"].startswith("Hi Bernd") and task["entities"]["draft"]["ref"] == "Draft ID: r123"
    proposal = store.get_proposal(result.proposals[0])
    assert proposal["params"]["draft"].startswith("Hi Bernd") and proposal["params"]["draft_ref"] == "Draft ID: r123"
    text = build_notice(task, [proposal], language="es")["text"]
    assert "Propongo responder" in text and "Could you send screenshots?" in text
    # A later bundle without a new draft still attaches the pending one; sending it clears it.
    replan = plan_answer(task_match={"decision": "advances", "task_id": task["id"], "why": "ci"}, task={**plan_answer()["task"], "kind": "work_item"},
                         proposals=[{"title": "Comprobar CI", "rationale": "", "action_class": "read", "operation": "investigate", "when": "", "urgency": "normal", "options": []}])
    again = act.apply(store, cfg, signals=[], extract=None, plan=reason.validate_plan(replan), now_dt=NOW + timedelta(hours=1), preferences=[], existing_task=store.get_task(task["id"]))
    assert store.get_proposal(again.proposals[0])["params"]["draft"].startswith("Hi Bernd")
    act.forget_draft(store, task["id"], "2026-10-19T12:00:00Z")
    assert "draft" not in store.get_task(task["id"])["entities"]


def test_receiver_follows_the_ingress_toggle_without_a_restart(store):
    cfg = config()
    rt = Runtime(FakeContext(cfg, llm=None), store, cfg, worker_id="w", clock=lambda: NOW)
    events: list[str] = []

    class Receiver:
        async def stop(self):
            events.append("stop")

    async def fake_start():
        events.append("start")
        rt.receiver = Receiver()
        return True

    rt.start_receiver = fake_start  # type: ignore[method-assign]

    async def scenario():
        rt.ensure_receiver()  # disabled: nothing happens
        await asyncio.sleep(0)
        assert events == [] and rt.receiver is None
        rt.config["ingress"]["gmail_receiver"]["enabled"] = True
        rt.ensure_receiver()
        rt.ensure_receiver()  # a second call while starting does not start twice
        await asyncio.sleep(0)
        assert events == ["start"] and rt.receiver is not None and rt.ctx.spawned == ["radar:gmail-receiver"]
        rt.config["ingress"]["gmail_receiver"]["enabled"] = False
        rt.ensure_receiver()
        await asyncio.sleep(0)
        assert events == ["start", "stop"] and rt.receiver is None

    asyncio.run(scenario())

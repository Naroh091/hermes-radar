"""Work before mail, questions before guesses: what Radar learned from four days of running.

A model's reading of a message is a hypothesis, the owner's word is a fact; an unanswered notice holds the
next one; a mute teaches; a long chat is never dropped for lasting; a worker process is not the owner;
and a work item is investigated and fixed, not replied to.
"""
from __future__ import annotations

from datetime import timedelta

from conftest import NOW, active_config, config, extract_answer, plan_answer, verify_destination

from radar.delivery.notice import build_notice
from radar.delivery.responses import _answer, act_on_task, respond_to_proposal
from radar.jobs.firewall import guard_tool_call
from radar.jobs.prompts import build_job_prompt
from radar.pipeline import act, correlate, extract as extract_step, reason
from radar.pipeline.checklists import template_for
from radar.pipeline.process import _bound_task_id, _synthetic_extract
from radar.pipeline.reason import fact_origin
from radar.pipeline.triage import triage_signal
from radar.preferences import subject_stem
from radar.runtime import job_budget
from radar.sources.session import record_turn

T = "2026-10-19T10:00:00Z"


def _email_signal(store, signal_id, *, sender, subject, group_key="thread:1", when=T):
    store.add_signal(signal_id=signal_id, source_id="src", kind="email", occurred_at=when, external_ref=signal_id, group_key=group_key,
                     title=subject, snippet="", meta={"sender": sender, "subject": subject, "account": "a@example.org"}, fingerprint=signal_id, now=when)
    return store.get_signal(signal_id)


def _source(store):
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=T)


def _apply(store, cfg, plan, *, extract=None, signals=None):
    return act.apply(store, cfg, signals=signals or [], extract=extract_step.validate_extract(extract or extract_answer()),
                     plan=reason.validate_plan(plan), now_dt=NOW, preferences=[])


def test_a_models_reading_is_a_capped_hypothesis_and_the_owners_word_wins(store):
    _source(store)
    signals = [_email_signal(store, "s1", sender="anais@unav.es", subject="Web Iberifier")]
    guessed = extract_answer(facts_learned=[{"key": "gestion_iberifier_eu", "value": "es externa", "confidence": 0.95}])
    _apply(store, config(), plan_answer(), extract=guessed, signals=signals)
    fact = store.list_facts()[0]
    assert fact["key"] == "gestion_iberifier_eu" and fact["confidence"] == 0.6 and fact_origin(fact) == "inferred"
    store.record_owner_fact(key="gestion_iberifier_eu", value="la gestiona el propietario", provenance={"at": T}, now=T)
    fact = store.list_facts()[0]
    assert fact["value"] == "la gestiona el propietario" and fact["confidence"] == 1.0 and fact_origin(fact) == "owner"
    store.upsert_fact(key="gestion_iberifier_eu", value="es externa", confidence=0.6, provenance={"origin": "inferred"}, now=T)
    assert store.list_facts()[0]["value"] == "la gestiona el propietario"  # a later guess never overrides the owner
    assert fact_origin({"key": "old", "value": "x"}) == "inferred"  # facts recorded before origins existed were guesses


def test_an_ask_records_the_owners_answer_as_a_fact_and_replans_the_task(store):
    cfg = active_config()
    store.create_task(task_id="t1", kind="work_item", title="Caída de la web de IBERIFIER", summary="", entities={}, expectations=[], checklist=[], now=T)
    plan = reason.validate_plan(plan_answer(proposals=[{
        "title": "¿La web de Iberifier la llevamos nosotros?", "rationale": "Nada lo dice", "action_class": "local", "operation": "ask",
        "when": "", "urgency": "high", "options": [{"id": "ours", "label": "Sí, la llevamos nosotros"}, {"id": "external", "label": "No, es externa"}],
        "fact": {"key": "gestion_iberifier_eu", "yes": "la gestiona el propietario", "no": "es externa, no la gestiona el propietario"},
    }]))
    ask = plan["proposals"][0]
    assert ask["operation"] == "ask" and ask["action_class"] == "read" and ask["fact"]["key"] == "gestion_iberifier_eu" and len(ask["options"]) == 2
    store.create_task_proposal(proposal_id="q1", task_id="t1", title=ask["title"], reason_text="", action_class="read", operation_id="ask",
                               params={"fact": ask["fact"]}, options=ask["options"], urgency="high", expires_at="2026-10-22T10:00:00Z", fingerprint="q1", now=T)
    payload = build_notice(store.get_task("t1"), [store.get_proposal("q1")], language="es")
    assert "<b>Confírmame:</b>" in payload["text"] and payload["buttons"][0][0]["label"].endswith("Sí, la llevamos nosotros")
    assert _answer(store, "q1", "yes") == "ours" and _answer(store, "q1", "no") == "external"
    answered = respond_to_proposal(store, cfg, proposal_id="q1", option="ours", actor="telegram:7", now_dt=NOW)
    assert answered["outcome"] == "answered" and "la gestiona el propietario" in answered["text"]
    fact = store.list_facts()[0]
    assert fact["value"] == "la gestiona el propietario" and fact_origin(fact) == "owner"
    reply = next(s for s in store.list_signals() if s["kind"] == "user_reply")
    assert reply["meta"]["task_id"] == "t1" and _bound_task_id([reply]) == "t1"
    synthetic = _synthetic_extract(reply, store.get_task("t1"))
    assert synthetic["owner_answer"]["answer"] == "Sí, la llevamos nosotros" and synthetic["reason"] == "owner_answer"
    assert store.get_proposal("q1")["status"] == "accepted"


def test_waiting_for_a_conversation_to_end_costs_no_attempt(store):
    store.ensure_local_observation_source(T, 30)
    store.add_signal(signal_id="c1", source_id="local:session-hooks", kind="session_turn", occurred_at=T, external_ref="c1", group_key="session:s",
                     title="hola", snippet="hola", meta={"session_id": "s"}, fingerprint="c1", now=T)
    for _ in range(6):
        [bundle] = store.claim_signal_bundles("2026-10-19T10:05:00Z", "2026-10-19T10:15:00Z", 5)
        store.defer_signals(bundle["signals"], next_attempt_at=T, error="session_active", now=T, counts_attempt=False)
    assert store.get_signal("c1")["attempts"] == 0  # six claims while the owner kept talking, nothing charged
    [bundle] = store.claim_signal_bundles("2026-10-19T10:05:00Z", "2026-10-19T10:15:00Z", 5)
    store.defer_signals(bundle["signals"], next_attempt_at=T, error="plan is not JSON", now=T)
    assert store.get_signal("c1")["attempts"] == 1  # a real failure still counts


def test_a_worker_process_is_not_the_owner_talking(store, monkeypatch):
    cfg = config()
    event = {"session_id": "k1", "turn_id": "a", "platform": "cli", "user_message": "Escanea el lote 3", "assistant_response": "Hecho.", "model": "m"}
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_123")
    assert record_turn(store, cfg, event, NOW) is False
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.setenv("HERMES_RADAR_JOB", "1")
    assert record_turn(store, cfg, event, NOW) is False
    monkeypatch.delenv("HERMES_RADAR_JOB")
    assert record_turn(store, cfg, event, NOW) is True


def test_an_unanswered_notice_holds_even_urgent_news_until_the_cooldown_passes(store):
    cfg = active_config()
    verify_destination(store)
    first = _apply(store, cfg, plan_answer(notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}))
    from radar.delivery.dispatch import dispatch

    assert dispatch(store, cfg, NOW)["notices"] == 1
    urgent_again = plan_answer(task_match={"decision": "advances", "task_id": first.task_id, "why": "same thread"},
                               proposals=[{"title": "Enviar otra respuesta", "rationale": "", "action_class": "external", "operation": "gmail.reply",
                                           "when": "", "urgency": "high", "options": [{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}]}],
                               notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"})
    act.apply(store, cfg, signals=[], extract=extract_step.validate_extract(extract_answer()), plan=reason.validate_plan(urgent_again),
              now_dt=NOW + timedelta(minutes=5), preferences=[])
    held = next(p for p in store.list_proposals(task_id=first.task_id) if p["title"] == "Enviar otra respuesta")
    assert held["batch_key"] == "digest"  # five minutes after an unanswered notice, "urgent" still waits
    assert act.interrupt_again(store, cfg, first.task_id, "now", NOW + timedelta(hours=4))  # after the cooldown it may knock again
    assert not act.interrupt_again(store, cfg, first.task_id, "today", NOW + timedelta(hours=4))


def test_a_mute_silences_the_whole_family_of_that_mail(store):
    cfg = active_config()
    _source(store)
    origin = _email_signal(store, "g1", sender="no-reply@accounts.google.com", subject="Alerta de seguridad")
    store.create_task(task_id="t1", kind="check", title="Verificar inicio de sesión de Google en Mac OS", summary="", entities={}, expectations=[], checklist=[], now=T,
                      origin_signal_id="g1")
    muted = act_on_task(store, cfg, task_id="t1", action="mute", actor="telegram:7", now_dt=NOW)
    assert muted["outcome"] == "muted" and "accounts.google.com" in muted["text"]
    prefs = store.list_preferences(active_only=True)
    assert len(prefs) == 1 and prefs[0]["scope"] == {"sender": "no-reply@accounts.google.com", "subject_stem": "alerta de seguridad", "kind": "email"}
    again = _email_signal(store, "g2", sender="no-reply@accounts.google.com", subject="Re: Alerta de seguridad", group_key="thread:2")
    decision = triage_signal(again, prefs, cfg)
    assert not decision.keep and decision.reason.startswith("preference:mute:")
    other = _email_signal(store, "g3", sender="no-reply@accounts.google.com", subject="Factura de Google Workspace", group_key="thread:3")
    assert triage_signal(other, prefs, cfg).keep  # the same sender on another subject still gets through
    assert subject_stem("[MalditaEs] A security advisory on markdown-it affects at least one of your repositories") == "a security advisory on markdown"
    assert subject_stem("Fwd: Re: Feed de Yahoo | foto del artículo 2026") == "feed de yahoo foto del"


def test_the_same_thread_is_the_same_task(store):
    _source(store)
    first = _email_signal(store, "a1", sender="noreply@github.com", subject="[MalditaEs] A security advisory on follow-redirects", group_key="thread:gh-1")
    store.create_task(task_id="t1", kind="check", title="Vulnerabilidad alta en follow-redirects", summary="", entities={}, expectations=[], checklist=[], now=T,
                      origin_signal_id="a1")
    store.link_task_signal("t1", "a1", "origin", T)
    extract = extract_step.validate_extract(extract_answer(title="Aviso de seguridad follow-redirects", summary="Segundo aviso", category="check"))
    candidates = correlate.candidate_tasks(store, extract, group_key="thread:gh-1")
    assert candidates and candidates[0]["id"] == "t1"


def test_a_private_calendar_entry_is_a_local_effect_but_an_invitation_is_not():
    local = {"HERMES_RADAR_JOB": "1", "HERMES_RADAR_JOB_CLASS": "local"}
    assert guard_tool_call(tool_name="mcp__google_calendar__create_event", args={"summary": "Acceder al expediente", "start": "2026-10-31T09:00"}, environ=local) is None
    assert guard_tool_call(tool_name="mcp__google_calendar__create_event", args={"summary": "Reunión", "attendees": ["x@y.es"]}, environ=local)["action"] == "block"
    read = {"HERMES_RADAR_JOB": "1", "HERMES_RADAR_JOB_CLASS": "read"}
    assert guard_tool_call(tool_name="mcp__google_calendar__create_event", args={"summary": "Nota"}, environ=read)["action"] == "block"


def test_a_work_item_is_investigated_and_fixed_not_replied_to():
    assert "reporter answered" not in template_for("work_item")
    task = {"kind": "work_item", "title": "Fallo de fotos en el feed de Yahoo", "summary": "", "entities": {"repo": "MalditaEs/factchequeado"}, "checklist": []}
    facts = [{"key": "gestion_iberifier_eu", "value": "es externa", "provenance": {"origin": "inferred"}},
             {"key": "dns_iberifier_eu", "value": "lo lleva el propietario en Cloudflare", "provenance": {"origin": "owner"}}]
    prompt = build_job_prompt(task=task, proposal={"title": "Reproducir el fallo del feed", "reason_text": "", "params": {}, "operation_id": "investigate"},
                              option="do", action_class="read", signals=[], facts=facts, owner={"language": "es"}, workdir="/tmp/wd")
    assert "HOW TO WORK A BUG" in prompt and "open every attachment" in prompt and "Cloudflare tools" in prompt
    assert "UNVERIFIED ASSUMPTIONS" in prompt and "gestion_iberifier_eu=es externa" in prompt
    assert "OWNER FACTS" in prompt and "dns_iberifier_eu=lo lleva el propietario" in prompt
    local = build_job_prompt(task=task, proposal=None, option="", action_class="local", signals=[], facts=[], owner={"language": "es"}, workdir=None)
    assert "the owner wants the work done, not mail about it" in local and "prepares a reply or message, save it as a draft" not in local
    assert "HOW TO WORK A BUG" in local
    trip = build_job_prompt(task={"kind": "trip", "title": "Viaje", "summary": "", "entities": {}, "checklist": []}, proposal=None, option="",
                            action_class="read", signals=[], facts=[], owner={"language": "es"}, workdir=None)
    assert "HOW TO WORK A BUG" not in trip
    assert "draft a reply" not in reason.PLAN_PROMPT and "work done, not mail written" in reason.PLAN_PROMPT
    assert '"ask" (read) is a question' in reason.PLAN_PROMPT and "guess what the facts do not say" in reason.PLAN_PROMPT


def test_an_investigation_gets_room_to_work():
    cfg = config()
    assert job_budget(cfg, {"kind": "proposal", "action_class": "read"}, {"kind": "work_item"}) == (60, 900)
    assert job_budget(cfg, {"kind": "proposal", "action_class": "local"}, {"kind": "work_item"}) == (60, 900)
    assert job_budget(cfg, {"kind": "proposal", "action_class": "read"}, {"kind": "trip"}) == (20, 300)
    assert job_budget(cfg, {"kind": "native", "action_class": "external"}, {"kind": "work_item"}) == (20, 300)
    assert job_budget(config(jobs={"investigate_turns": 80, "investigate_seconds": 1200}), {"kind": "proposal", "action_class": "read"}, {"kind": "work_item"}) == (80, 1200)

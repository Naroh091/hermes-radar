"""A chat session is judged as a whole, never one turn at a time; credentials never reach the store."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from conftest import NOW, FakeContext, FakeLlm, active_config, config, extract_answer, plan_answer, verify_destination

from radar.pipeline import act, correlate, extract as extract_step, reason
from radar.pipeline.conversation import transcript
from radar.pipeline.process import _auto_eligible, process_bundle
from radar.redact import redact
from radar.runtime import Runtime
from radar.scheduler import ensure_routines
from radar.sources.session import SOURCE_ID, record_turn

NOW_S = "2026-10-19T10:00:00Z"
SID = "20261020_004547_a14ccd"
FB_TOKEN = "EAAGFakeMetaToken0FakeMetaToken0FakeMetaToken0FakeMetaToken0FakeMetaToken0FakeMetaToken0FakeMetaToken0FakeMetaToken0FakeMetaToken0FakeMetaToken0"  # synthetic, shaped like a Meta Graph token
JWT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJpYXQiOjE3OTI0NTAwOTAsInJvbGVzIjpbIlJPTEVfQVBJX1dSSVRFIl19.EpTwRUnHhyzSGLFpQcUANIbXRtobPY"


def _turn(store, signal_id, at, user_text, assistant_text="", sid=SID):
    store.add_signal(signal_id=signal_id, source_id=SOURCE_ID, kind="session_turn", occurred_at=at, external_ref=signal_id, group_key=f"session:{sid}",
                     title=user_text[:80], snippet=user_text, meta={"session_id": sid, "platform": "desktop", "user_text": user_text, "assistant_text": assistant_text},
                     fingerprint=signal_id, now=at)


def _runtime(store, cfg, llm, tmp_path, clock):
    return Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=clock, hermes_home=str(tmp_path))


def test_pasted_credentials_never_reach_the_store_or_the_model(store):
    cfg = config()
    event = {"session_id": SID, "platform": "desktop", "turn_id": "t1", "user_message": f"prueba con {FB_TOKEN}",
             "assistant_response": f"Token aplicado. Cabecera Authorization: Bearer {JWT} y commit 8db4e1b4d2e43a51cd0e1e7973019c58b8d180d7."}
    assert record_turn(store, cfg, event, NOW) is True
    stored = json.dumps(store.list_signals(kind="session_turn")[0], ensure_ascii=False)
    assert FB_TOKEN not in stored and JWT not in stored and "[redacted]" in stored and "8db4e1b4d2e43a51cd0e1e7973019c58b8d180d7" in stored
    # Rows written before redaction existed are still masked on their way to the model.
    _turn(store, "old", "2026-10-19T09:00:00Z", f"mira maldita con {FB_TOKEN}", "Ese token no es válido.")
    view = extract_step.signal_view(store.get_signal("old"), budget=4000)
    assert FB_TOKEN not in view["user_said"] and "[redacted]" in view["user_said"]
    assert redact("sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") == "[redacted]"
    assert redact("el pedido 1234567890 y el evento 01a111a7-41d8-76b2-a1c1-dfeb156b72b9") == "el pedido 1234567890 y el evento 01a111a7-41d8-76b2-a1c1-dfeb156b72b9"


def test_the_whole_session_travels_with_new_turns_and_a_finished_one_closes_what_it_opened(store, tmp_path):
    cfg = active_config()
    verify_destination(store)
    store.ensure_local_observation_source(NOW_S, 30)
    # An earlier bundle of this session already opened a task; its turn stays as context.
    _turn(store, "a1", "2026-10-19T09:00:00Z", "me gustaría que vincules estos claims con el evento de Francia", "Voy con ello.")
    store.finish_signals([store.get_signal("a1")], status="extracted", reason="new", now="2026-10-19T09:40:00Z")
    store.create_task(task_id="task-a", kind="work_item", title="Vincular claims al evento", summary="", entities={"session_id": SID}, expectations=[],
                      checklist=[], now="2026-10-19T09:40:00Z", origin_signal_id="a1")
    _turn(store, "a2", "2026-10-19T09:20:00Z", "genial, ¿hecho?", "Hecho. 11 claims vinculados y verificados por SQL y API.")
    closing = plan_answer(task_match={"decision": "completes", "task_id": "task-a", "why": "the chat finished it"},
                          task={"kind": "work_item", "title": "Vincular claims al evento", "summary": "Hecho en la sesión.", "status": "done", "due_at": "", "next_review_at": "", "confidence": 0.9},
                          checklist=[], proposals=[], notify={"disposition": "digest", "urgency": "whenever", "importance": "low", "reason": "done"},
                          escalation={"decision": "inform", "why": "news"}, message="Quedaron vinculados los 11 claims.")
    llm = FakeLlm({"radar.extract": [extract_answer(relevant=False, category="noise", conversation_state="resolved")], "radar.plan": [closing]})
    later = NOW + timedelta(minutes=5)
    rt = _runtime(store, cfg, llm, tmp_path, lambda: later)
    bundle = store.claim_signal_bundles("2026-10-19T10:05:00Z", "2026-10-19T10:15:00Z", 5)[0]
    assert [s["id"] for s in bundle["signals"]] == ["a2"]
    outcome = asyncio.run(process_bundle(rt, bundle, later))
    payload = json.loads(llm.calls[0]["input"][0]["text"])
    turns = payload["conversation"]["turns"]
    assert payload["signals"] == [] and payload["conversation"]["session_id"] == SID
    assert [t["new"] for t in turns] == [False, True] and turns[0]["owner_said"].startswith("me gustaría") and turns[1]["agent_said"].startswith("Hecho.")
    # The task closes, nothing is proposed and nobody is told: the owner was in that chat.
    assert outcome["decision"] == "completes" and outcome["disposition"] == "silent"
    assert store.get_task("task-a")["status"] == "done" and store.list_proposals(task_id="task-a") == []
    assert store.get_signal("a2")["triage_status"] == "extracted"


def test_a_resolved_conversation_without_a_task_leaves_nothing_behind(store, tmp_path):
    cfg = active_config()
    store.ensure_local_observation_source(NOW_S, 30)
    _turn(store, "q1", "2026-10-19T09:00:00Z", "calificamos para el nonprofit de tailscale?", "Probablemente sí: 50 % para entidades sin ánimo de lucro reconocidas.")
    _turn(store, "q2", "2026-10-19T09:02:00Z", "pero no me refiero a hay derecho sino a Fundación Maldita.es", "Tienes razón; el criterio parece aplicable, no lo he comprobado.")
    llm = FakeLlm({"radar.extract": [extract_answer(relevant=False, category="noise", conversation_state="resolved")]})
    rt = _runtime(store, cfg, llm, tmp_path, lambda: NOW)
    bundle = store.claim_signal_bundles(NOW_S, "2026-10-19T10:10:00Z", 5)[0]
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome["result"] == "noise" and store.list_tasks() == []
    assert {store.get_signal(i)["triage_reason"] for i in ("q1", "q2")} == {"conversation_resolved"}
    # Even when the extractor calls a resolved conversation relevant, the planner cannot start a task from it.
    _turn(store, "q3", "2026-10-19T10:20:00Z", "vale, gracias, con eso me basta por hoy", "De nada.")
    llm = FakeLlm({"radar.extract": [extract_answer(category="other", conversation_state="resolved")], "radar.plan": [plan_answer()]})
    rt = _runtime(store, cfg, llm, tmp_path, lambda: NOW + timedelta(hours=1))
    bundle = store.claim_signal_bundles("2026-10-19T11:00:00Z", "2026-10-19T11:10:00Z", 5)[0]
    outcome = asyncio.run(process_bundle(rt, bundle, NOW + timedelta(hours=1)))
    assert outcome["result"] == "resolved" and store.list_tasks() == [] and store.get_signal("q3")["triage_reason"] == "conversation_resolved"


def test_conversation_candidates_are_the_sessions_own_tasks_not_any_task_of_the_same_kind(store):
    now = NOW_S
    store.create_task(task_id="hub", kind="work_item", title="Restaurar dms_maldita y montar The Hub en local", summary="dump en curso", entities={"session_id": "other-session"},
                      expectations=[], checklist=[], now=now)
    store.create_task(task_id="mine", kind="work_item", title="Vincular claims al evento de Francia", summary="", entities={"session_id": SID}, expectations=[], checklist=[], now=now)
    extract = extract_step.validate_extract(extract_answer(category="work_item", title="Acceso a la API de maldita", summary="Guardar el token en BWS", entities={}, conversation_state="open"))
    assert [c["id"] for c in correlate.candidate_tasks(store, extract, session_id=SID)] == ["mine"]
    assert {c["id"] for c in correlate.candidate_tasks(store, extract)} == {"hub", "mine"}  # other bundles keep the same-kind fallback


def test_chat_born_tasks_get_no_automatic_jobs_unless_configured(store, tmp_path):
    cfg = active_config(trust={"level": "autonomous", "overrides": {}, "auto_external": []})
    rt = _runtime(store, cfg, FakeLlm({}), tmp_path, lambda: NOW)
    proposal = {"operation_id": "investigate", "action_class": "read", "status": "pending"}
    chat_task = {"id": "t", "kind": "work_item", "entities": {"session_id": SID}}
    assert _auto_eligible(rt, proposal, chat_task) is False
    assert _auto_eligible(rt, proposal, {"id": "m", "kind": "work_item", "entities": {}}) is True
    cfg["observation"]["session_auto_jobs"] = True
    assert _auto_eligible(rt, proposal, chat_task) is True


def test_tasks_born_before_session_ids_existed_are_repaired_on_start(store):
    store.ensure_local_observation_source(NOW_S, 30)
    _turn(store, "o1", "2026-10-19T09:00:00Z", "actualiza el Langfuse de Coolify a la versión 4", "Hecho.", sid="20261020_002521_9f4ab4")
    store.create_task(task_id="lf", kind="work_item", title="Completar la migración de Langfuse v4", summary="", entities={}, expectations=[], checklist=[], now=NOW_S, origin_signal_id="o1")
    ensure_routines(store, NOW_S)
    assert store.get_task("lf")["entities"]["session_id"] == "20261020_002521_9f4ab4"
    assert store.backfill_task_sessions(NOW_S) == 0 and [t["id"] for t in store.tasks_for_session("20261020_002521_9f4ab4")] == ["lf"]


def test_mail_the_owner_wrote_is_never_reported_back_to_them(store):
    cfg = config(sources=[{"id": "gmail-work", "kind": "gmail", "address": "me@example.org"}])
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=NOW_S)
    store.add_signal(signal_id="e1", source_id="src", kind="email", occurred_at=NOW_S, external_ref="m1", group_key="thread:x", title="Re: Invitación aniversario",
                     snippet="Me he apuntado, pasaré a las 18:30", meta={"sender": "David <me@example.org>", "account": "me@example.org"}, fingerprint="f", now=NOW_S)
    plan = plan_answer(task={"kind": "commitment", "title": "Decidir si asistir al aniversario", "summary": "Confirmó asistencia.", "status": "done", "due_at": "", "next_review_at": "", "confidence": 0.9},
                       proposals=[], notify={"disposition": "digest", "urgency": "whenever", "importance": "low", "reason": "x"}, escalation={"decision": "inform", "why": "x"},
                       message="Te has apuntado al aniversario.")
    result = act.apply(store, cfg, signals=[store.get_signal("e1")], extract=extract_step.validate_extract(extract_answer(category="commitment")),
                       plan=reason.validate_plan(plan), now_dt=NOW, preferences=[])
    assert result.disposition == "silent" and store.list_proposals(task_id=result.task_id) == [] and store.get_task(result.task_id)["status"] == "done"
    # The same news from someone else still reaches the owner, grouped for the morning.
    store.add_signal(signal_id="e2", source_id="src", kind="email", occurred_at=NOW_S, external_ref="m2", group_key="thread:y", title="Pago recibido",
                     snippet="Has cobrado 1.500 €", meta={"sender": "Payoneer <no-reply@payoneer.com>", "account": "me@example.org"}, fingerprint="g", now=NOW_S)
    plan["task"]["title"] = "Cobro de Payoneer"
    result = act.apply(store, cfg, signals=[store.get_signal("e2")], extract=extract_step.validate_extract(extract_answer(category="commitment")),
                       plan=reason.validate_plan(plan), now_dt=NOW, preferences=[])
    inform = store.list_proposals(task_id=result.task_id)
    assert result.disposition == "digest" and len(inform) == 1 and inform[0]["operation_id"] == "inform" and inform[0]["params"]["deliver_after"] == "2026-10-20T07:00:00Z"


def test_transcript_keeps_new_turns_and_trims_old_context_to_the_budget(store):
    store.ensure_local_observation_source(NOW_S, 30)
    for index in range(6):
        _turn(store, f"c{index}", f"2026-10-19T09:{index:02d}:00Z", f"turno {index} " + "x" * 300, "y" * 300)
    turns = transcript(store, SID, {"c5"}, max_chars=2000)
    assert [t["new"] for t in turns] == [False, False, True] and turns[0]["owner_said"].startswith("turno 3")

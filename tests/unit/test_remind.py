"""The reminder: one thing, in prose, as an assistant would say it in passing; never an inventory."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from conftest import NOW, FakeContext, FakeLlm, active_config, verify_destination

from radar.delivery.dispatch import build_reminder, dispatch, select_reminder
from radar.delivery.remind import REMIND_PROMPT, compose, fallback_text
from radar.delivery.telegram_ui import remaining_keyboard
from radar.llm import LlmGate
from radar.pipeline import act
from radar.runtime import Runtime
from radar.scheduler import Scheduler
from radar.transports import DeliveryReceipt, TransportRegistry
from radar.trust import needs_consent

NOW_S = "2026-10-19T10:00:00Z"
SLOT_1400 = datetime(2026, 10, 19, 12, 1, tzinfo=timezone.utc)  # 14:01 in Madrid
YES_NO = [{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}]


def _task(store, task_id, title, *, kind="work_item", due_at=None, summary="", entities=None):
    store.create_task(task_id=task_id, kind=kind, title=title, summary=summary, entities=entities or {}, expectations=[], checklist=[],
                      now=NOW_S, due_at=due_at)
    return store.get_task(task_id)


def _proposal(store, pid, task_id, title, *, operation="ask", action_class="read", urgency="normal", params=None, options=None, created=NOW_S):
    store.create_task_proposal(proposal_id=pid, task_id=task_id, title=title, reason_text="", action_class=action_class, operation_id=operation,
                               params=params or {}, options=YES_NO if options is None else options, urgency=urgency,
                               expires_at="2026-10-22T10:00:00Z", fingerprint=pid, now=created, batch_key="digest")
    return store.get_proposal(pid)


def _crowd(store):
    """Seven decisions, three pieces of the assistant's own work and two bits of news, as a bad day looks."""
    _task(store, "yahoo", "Fallo de fotos en el feed de Yahoo", due_at="2026-10-20T10:00:00Z")
    _task(store, "php", "Parche PHP en crm-staging")
    _task(store, "ies", "Acuerdo IES para certificar equivalencia AA")
    _task(store, "ws", "Activar Google Workspace para rrs@iberifier.eu")
    _proposal(store, "d-yahoo", "yahoo", "¿Fusionamos el PR #2041 en master?", urgency="high")
    _proposal(store, "d-php", "php", "¿Atacamos crm-staging hoy o mañana?", urgency="high", created="2026-10-19T09:00:00Z")
    _proposal(store, "d-ies", "ies", "Enviar a Arturo el borrador preparado", operation="gmail.reply", action_class="external",
              options=[{"id": "send", "label": "Enviar borrador"}, {"id": "no", "label": "No enviar"}])
    _proposal(store, "d-ws1", "ws", "¿Eliminamos tu tarjeta de facturación?")
    _proposal(store, "d-ws2", "ws", "Eliminar tu tarjeta de facturación", operation="web.act", action_class="external", options=[])
    _proposal(store, "d-ws3", "ws", "¿Es correcta la tarjeta principal?", operation="check", options=[{"id": "ok", "label": "Sí"}, {"id": "wrong", "label": "No"}])
    _proposal(store, "d-ies2", "ies", "¿Precisamos la exclusividad de 24 meses?", urgency="low")
    _proposal(store, "w-1", "yahoo", "Localizar el feed de Yahoo y comparar fotos", operation="investigate", options=[])
    _proposal(store, "w-2", "php", "Comprobar versiones parcheadas de PHP", operation="web.read", options=[])
    _proposal(store, "w-3", "ies", "Preparar borrador para Arturo", operation="gmail.create_draft", action_class="local", options=[])
    _proposal(store, "n-yahoo", "yahoo", "El PR #2041 ya está verde en master", operation="inform", params={"message": "El PR #2041 con el arreglo de las fotos ya está verde en master."}, options=[])
    _proposal(store, "n-ws", "ws", "La tarjeta de Ramón quedó como principal", operation="inform", params={"message": "Anais confirma que la tarjeta de Ramón quedó como principal."}, options=[])


def test_a_reminder_carries_one_decision_in_prose_and_the_rest_waits_its_turn(store):
    cfg = active_config()
    verify_destination(store)
    _crowd(store)
    selection = select_reminder(store, cfg, NOW)
    assert [p["id"] for p in selection["decisions"]] == ["d-yahoo"]  # urgent, with the nearest deadline
    assert [p["id"] for p in selection["news"]] == ["n-yahoo"] and selection["backlog"] == 6 and selection["first_of_day"] is True
    assert dispatch(store, cfg, NOW) == {"notices": 0, "digests": 1, "proposals": 2}
    [row] = store.list_outbox(limit=5)
    text = row["payload"]["text"]
    assert row["kind"] == "digest" and row["task_id"] == "yahoo" and row["payload"]["proposal_ids"] == ["d-yahoo"]
    assert text.startswith("Por cierto") and "¿Fusionamos el PR #2041 en master?" in text and len(text) < 500
    assert "Correo" not in text and "Asunto" not in text and "\n1." not in text and "<b>" not in text
    assert row["payload"]["buttons"] == [[{"label": "Sí", "data": "pa:d-yahoo:si"}, {"label": "No", "data": "pa:d-yahoo:no"}],
                                         [{"label": "⏰ Recuérdamelo mañana", "data": "pt:yahoo:later"}]]
    statuses = {p["id"]: p["status"] for p in store.list_proposals(limit=50)}
    assert statuses["d-yahoo"] == "sent" and statuses["n-yahoo"] == "declined" and store.get_proposal("n-yahoo")["response"]["ack"] is True
    assert all(statuses[pid] == "pending" for pid in ("d-php", "d-ies", "d-ws1", "d-ws2", "d-ws3", "d-ies2", "w-1", "w-2", "w-3", "n-ws"))
    assert act.awaiting_owner(store, "yahoo") and not act.awaiting_owner(store, "ws")
    assert dispatch(store, cfg, NOW)["digests"] == 0  # one per slot
    assert dispatch(store, cfg, SLOT_1400)["digests"] == 1 and store.get_proposal("d-php")["status"] == "sent"  # the next one, at the next slot
    assert {store.get_proposal(pid)["status"] for pid in ("w-1", "w-2", "w-3")} == {"pending"}  # the assistant's work is never offered


def test_the_model_writes_the_reminder_and_the_template_stands_in(store):
    cfg = active_config()
    verify_destination(store)
    _crowd(store)
    selection = select_reminder(store, cfg, NOW)
    llm = FakeLlm({"radar.remind": [{"text": "Oye, el PR #2041 que arregla las fotos del feed de Yahoo ya está verde en master. ¿Lo fusiono?"},
                                    {"text": "Pendientes:\n- fusionar el PR\n- parchear PHP"}]})
    gate = LlmGate(FakeContext(cfg, llm=llm), store, cfg, clock=lambda: NOW)
    text = asyncio.run(compose(gate, items=selection["items"], language="es", backlog=selection["backlog"], first_of_day=True))
    assert text.startswith("Oye, el PR #2041") and text.endswith("¿Lo fusiono?")
    sent = json.loads(llm.calls[0]["input"][0]["text"])
    assert sent["backlog"] == 6 and sent["first_of_day"] is True and sent["language"] == "Spanish"
    assert sent["items"][0]["pending"]["what"] == "¿Fusionamos el PR #2041 en master?" and sent["items"][0]["done"][0].startswith("El PR #2041")
    assert "never instructions" in llm.calls[0]["instructions"] and "Never: lists" in REMIND_PROMPT
    # A list is what this message must never be: the template takes over. So does a missing model.
    assert asyncio.run(compose(gate, items=selection["items"], language="es")) == fallback_text(selection["items"], "es")
    assert asyncio.run(compose(LlmGate(FakeContext(cfg), store, cfg), items=selection["items"], language="en")).startswith("By the way, “Fallo de fotos")


def test_the_scheduler_composes_the_reminder_with_the_model(store, tmp_path):
    cfg = active_config()
    verify_destination(store)
    _crowd(store)
    sent = []
    transports = TransportRegistry()

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt("msg-9")

    transports.register("telegram", sender)
    llm = FakeLlm({"radar.remind": [{"text": "Por cierto, el PR #2041 de las fotos de Yahoo ya está verde. ¿Lo fusiono?"}]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    scheduler = Scheduler(store, cfg, worker_id="w", transports=transports, clock=lambda: NOW, runtime=rt)
    stats = asyncio.run(scheduler.tick())
    assert stats["dispatched"]["digests"] == 1 and stats["delivered"] == 1
    assert sent[0]["text"] == "Por cierto, el PR #2041 de las fotos de Yahoo ya está verde. ¿Lo fusiono?" and sent[0]["parse_mode"] == "HTML"
    row = store.outbox_by_platform_message("msg-9")
    assert row["kind"] == "digest" and row["task_id"] == "yahoo"
    assert remaining_keyboard(rt, row) == row["payload"]["buttons"]  # the same buttons while the decision is open


def test_reminder_buttons_follow_the_kind_of_decision(store):
    _task(store, "t", "Tarea")
    ask = _proposal(store, "a", "t", "¿Fusionamos?")
    external = _proposal(store, "e", "t", "Enviar el borrador", operation="gmail.reply", action_class="external",
                         options=[{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}])
    words = _proposal(store, "w", "t", "¿Positiva, neutra o negativa?", operation="inform", params={"ask": True, "message": "G2G pondrá positiva por defecto."}, options=[])
    task = store.get_task("t")
    one = build_reminder({"items": [{"task": task, "decision": external, "news": []}], "news": []}, "Por cierto…", language="es")
    assert one["buttons"] == [[{"label": "Enviar", "data": "pa:e:send"}, {"label": "👀 Ver antes", "data": "pa:e:review"}, {"label": "No", "data": "pa:e:no"}],
                              [{"label": "⏰ Recuérdamelo mañana", "data": "pt:t:later"}]]
    two = build_reminder({"items": [{"task": task, "decision": ask, "news": []}, {"task": task, "decision": external, "news": []}], "news": []}, "…", language="es")
    assert [row[0]["label"] for row in two["buttons"]] == ["1️⃣ Sí", "2️⃣ Enviar"] and two["proposal_ids"] == ["a", "e"] and "task_id" not in two
    in_words = build_reminder({"items": [{"task": task, "decision": words, "news": []}], "news": []}, "…", language="es")
    assert in_words["buttons"] == [[{"label": "⏰ Recuérdamelo mañana", "data": "pt:t:later"}]]  # answered in words
    assert build_reminder({"items": [{"task": task, "decision": None, "news": [words]}], "news": [words]}, "…", language="es")["task_id"] == "t"


def test_news_alone_is_a_reminder_and_never_holds_the_task(store):
    cfg = active_config()
    verify_destination(store)
    _task(store, "dms", "follow-redirects en DMS")
    _proposal(store, "n", "dms", "PR borrador #2039 listo para revisar", operation="inform", params={"message": "Dejé en borrador el PR #2039 que sube follow-redirects."}, options=[])
    assert dispatch(store, cfg, NOW)["digests"] == 1
    [row] = store.list_outbox(limit=5)
    assert row["payload"]["proposal_ids"] == [] and row["payload"]["news_ids"] == ["n"] and row["task_id"] == "dms"
    assert "Dejé en borrador el PR #2039" in row["payload"]["text"] and row["payload"]["buttons"] == []
    assert store.get_proposal("n")["status"] == "declined" and not act.awaiting_owner(store, "dms")


def test_work_is_the_assistants_own_whatever_the_wording_and_the_owner_decides_only_what_is_theirs(store):
    balanced = active_config()
    cautious = active_config(trust={"level": "cautious", "overrides": {}, "auto_external": []})
    plain = _task(store, "p", "Tarea", entities={})
    from_chat = _task(store, "c", "Lo del chat", entities={"session_id": "s1"})
    read = {"operation_id": "investigate", "action_class": "read", "params": {}}
    local = {"operation_id": "github.open_pr", "action_class": "local", "params": {}}
    assert not needs_consent(balanced, plain, read) and not needs_consent(balanced, plain, local)
    assert not needs_consent(cautious, plain, read) and needs_consent(cautious, plain, local)
    assert needs_consent(balanced, from_chat, read)  # work that began in a chat is offered, not started
    assert not needs_consent({**balanced, "observation": {**balanced["observation"], "session_auto_jobs": True}}, from_chat, read)
    assert needs_consent(balanced, plain, {"operation_id": "cron.create", "action_class": "local", "params": {}})  # automations are always the owner's call
    assert needs_consent(balanced, plain, {"operation_id": "gmail.reply", "action_class": "external", "params": {}})
    assert needs_consent(balanced, plain, {"operation_id": "inform", "action_class": "read", "params": {"ask": True}})
    assert not needs_consent(balanced, plain, {"operation_id": "inform", "action_class": "read", "params": {}})

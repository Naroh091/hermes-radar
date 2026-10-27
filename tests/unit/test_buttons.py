"""Buttons that say what they do, one notice per open question, and chats judged whole even when deferred piecemeal."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from conftest import NOW, active_config, config, extract_answer, plan_answer, verify_destination

from radar.delivery.dispatch import dispatch
from radar.delivery.notice import build_notice
from radar.delivery.responses import act_on_task, resolve_reply
from radar.delivery.telegram_ui import handle_text_reply
from radar.pipeline import act, extract as extract_step, reason
from radar.scheduler import Scheduler
from radar.sources.session import record_turn
from radar.transports import DeliveryReceipt, TransportRegistry


def _apply(store, cfg, plan, *, extract=None, preferences=()):
    return act.apply(store, cfg, signals=[], extract=extract_step.validate_extract(extract or extract_answer()),
                     plan=reason.validate_plan(plan), now_dt=NOW, preferences=list(preferences))


def test_a_session_deferred_turn_by_turn_is_still_claimed_whole(store):
    cfg = config()
    first = {"session_id": "s1", "turn_id": "a", "platform": "telegram", "user_message": "podemos desplegar deepseek en mcp-tools?", "assistant_response": "Sí, faltan dos decisiones.", "model": "m"}
    assert record_turn(store, cfg, first, NOW)
    assert record_turn(store, cfg, {**first, "turn_id": "b", "user_message": "no limites las llamadas diarias, por favor"}, NOW + timedelta(minutes=10))
    due = {s["id"]: s["next_attempt_at"] for s in store.list_signals()}
    assert sorted(due.values()) == ["2026-10-19T10:02:00Z", "2026-10-19T10:12:00Z"]
    bundles = store.claim_signal_bundles("2026-10-19T10:05:00Z", "2026-10-19T10:15:00Z", 5)
    assert [len(b["signals"]) for b in bundles] == [2]  # the later turn travels with the due one
    assert store.claim_signal_bundles("2026-10-19T10:05:00Z", "2026-10-19T10:15:00Z", 5) == []  # all leased


def test_a_protected_risk_flag_keeps_news_out_of_silence_not_out_of_the_summary(store):
    cfg = config()
    risky = extract_answer(risk_flags=["financial_loss"], disposition="silent")
    ask = {"title": "¿Reclamo el cargo?", "rationale": "", "action_class": "read", "operation": "ask", "when": "", "urgency": "normal",
           "options": [{"id": "si", "label": "Sí, reclama"}, {"id": "no", "label": "No"}]}
    weekly = _apply(store, cfg, plan_answer(proposals=[ask] + plan_answer()["proposals"],
                                            notify={"disposition": "silent", "urgency": "this_week", "importance": "normal", "reason": "x"}), extract=risky)
    assert weekly.attention_reason == "protected_exception" and weekly.disposition == "digest"
    by_operation = {p["operation_id"]: p["batch_key"] for p in store.list_proposals(task_id=weekly.task_id)}
    assert by_operation["ask"] == "digest" and by_operation["web.read"] == "auto"  # the decision waits for the summary; the work runs by itself
    urgent = _apply(store, cfg, plan_answer(task={**plan_answer()["task"], "title": "Cargo no reconocido"},
                                            notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}), extract=risky)
    assert urgent.disposition == "notify_now"


def test_an_unanswered_notice_holds_what_comes_next_for_the_summary(store):
    cfg = active_config()
    verify_destination(store)
    send = {"title": "Enviar la respuesta a la agencia", "rationale": "", "action_class": "external", "operation": "gmail.reply", "when": "", "urgency": "normal",
            "options": [{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}]}
    first = _apply(store, cfg, plan_answer(proposals=[send], notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}))
    assert dispatch(store, cfg, NOW)["notices"] == 1
    assert act.awaiting_owner(store, first.task_id)
    louder = [{"id": "pref-loud", "status": "active", "dimension": "notification", "effect": "notify", "scope": {}, "origin": "explicit", "exceptions": []}]
    plan = plan_answer(task_match={"decision": "advances", "task_id": first.task_id, "why": "same trip"},
                       proposals=[send, {"title": "Reservar el hotel más barato", "rationale": "", "action_class": "external", "operation": "booking.reserve", "when": "", "urgency": "normal",
                                         "options": [{"id": "book", "label": "Reservar"}, {"id": "no", "label": "No"}]}],
                       notify={"disposition": "notify_now", "urgency": "today", "importance": "normal", "reason": "x"})
    again = _apply(store, cfg, plan, preferences=louder)
    assert again.attention_reason == "preference_notify" and again.task_id == first.task_id
    newest = next(p for p in store.list_proposals(task_id=first.task_id) if p["title"].startswith("Reservar"))
    assert newest["batch_key"] == "digest" and newest["params"]["deliver_after"] == "2026-10-19T12:00:00Z"  # 14:00 local
    assert dispatch(store, cfg, NOW)["notices"] == 0


def test_notice_separates_questions_from_steps_and_numbers_them_in_order(store):
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="purchase", title="Zapatillas talla 48", summary="Pedido confirmado.", entities={}, expectations=[], checklist=[], now=now)

    def proposal(pid, title, operation, options):
        store.create_task_proposal(proposal_id=pid, task_id="t1", title=title, reason_text="", action_class="read", operation_id=operation,
                                   params={}, options=options, urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint=pid, now=now)
        return store.get_proposal(pid)

    step = proposal("p-step", "Pedir el cambio de talla", "web.read", [])
    check = proposal("p-check", "¿Es correcta la talla 48 del pedido?", "check", [{"id": "ok", "label": "👍 Sí, es correcto"}, {"id": "wrong", "label": "⚠️ No, es un error"}])
    news = proposal("p-news", "Ha llegado la confirmación", "inform", [{"id": "ok", "label": "👍 Vale"}])
    payload = build_notice(store.get_task("t1"), [step, check, news], language="es")
    text = payload["text"]
    assert text.index("<b>Confírmame:</b>") < text.index("1. ¿Es correcta") < text.index("<b>Puedo hacer:</b>") < text.index("2. Pedir el cambio")
    assert payload["proposal_ids"] == ["p-news", "p-check", "p-step"]  # the numbers match a reply like «la 2»
    assert "Responde a este mensaje si prefieres" in text
    rows = payload["buttons"]
    assert rows[0][0]["label"] == "👍 Vale" and rows[1][0]["label"] == "1️⃣ 👍 Sí, es correcto" and rows[2][0]["label"] == "2️⃣ ✅ Adelante" and rows[2][1]["label"] == "❌ Descartar"
    assert [b["data"] for b in rows[-2]] == ["pt:t1:done", "pt:t1:later"] and rows[-1][0]["data"] == "pt:t1:mute"
    only_news = build_notice(store.get_task("t1"), [news], language="es")
    assert "Responde a este mensaje" not in only_news["text"] and "Puedo hacer" not in only_news["text"]


def test_task_buttons_say_when_they_come_back(store):
    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Decidir el alcance", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Paso", reason_text="", action_class="read", operation_id="web.read", params={},
                               options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="p1", now=now)
    later = act_on_task(store, cfg, task_id="t1", action="later", actor="telegram:7", now_dt=NOW)
    assert later["text"] == "Te lo recuerdo mañana a las 09:00: Decidir el alcance"
    assert store.get_task("t1")["snoozed_until"] == "2026-10-20T07:00:00Z" and store.get_proposal("p1")["snoozed_until"] == "2026-10-20T07:00:00Z"
    assert store.wake_snoozed_proposals("2026-10-20T07:00:00Z") == 1
    paced = active_config(notifications={"digest_times": [], "quiet_start": "22:00", "quiet_end": "08:00"})
    assert act_on_task(store, paced, task_id="t1", action="later", actor="telegram:7", now_dt=NOW)["text"] == "Te lo recuerdo mañana a las 08:00: Decidir el alcance"
    done = act_on_task(store, cfg, task_id="t1", action="done", actor="telegram:7", now_dt=NOW)
    assert done["text"] == "Marcado como resuelto: Decidir el alcance" and store.get_task("t1")["status"] == "done"


def test_a_reply_that_is_not_a_plain_order_reaches_the_agent(store):
    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Decidir el alcance", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Restringir el acceso a un grupo", reason_text="", action_class="read", operation_id="web.read",
                               params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="p1", now=now, batch_key="now")
    verify_destination(store)
    transports = TransportRegistry()
    sent = []

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt("msg-1")

    transports.register("telegram", sender)
    dispatch(store, cfg, NOW)
    assert asyncio.run(Scheduler(store, cfg, worker_id="w", transports=transports, clock=lambda: NOW).tick())["delivered"] == 1
    rt = SimpleNamespace(config=cfg, store=store, clock=lambda: NOW, transports=transports)

    def event(text):
        return SimpleNamespace(source=SimpleNamespace(platform="telegram", user_id="7", chat_id="42"), reply_to_message_id="msg-1", text=text)

    assert asyncio.run(handle_text_reply(rt, event("mejor restríngelo al equipo de datos"))) is None  # the agent takes it, with the proposal in context
    assert store.get_proposal("p1")["status"] == "sent"
    handled = asyncio.run(handle_text_reply(rt, event("no")))
    assert handled == {"action": "skip", "reason": "radar reply handled: declined"} and store.get_proposal("p1")["status"] == "declined"


def test_a_written_yes_or_no_answers_a_question_as_such(store):
    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="c1", kind="purchase", title="Zapatillas", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="chk", task_id="c1", title="¿Es correcta la talla 48?", reason_text="", action_class="read", operation_id="check",
                               params={}, options=[{"id": "ok", "label": "👍 Sí, es correcto"}, {"id": "wrong", "label": "⚠️ No, es un error"}],
                               urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="chk", now=now)
    row = {"task_id": "c1", "payload": {"proposal_ids": ["chk"], "task_id": "c1"}}
    from radar.delivery.feedback import classify

    assert classify("no")["intent"] == "reject" and classify("no, descártalo")["intent"] == "reject"
    wrong = resolve_reply(store, cfg, outbox_row=row, intent=classify("no"), actor="telegram:7", now_dt=NOW, note="no")
    assert wrong["outcome"] == "accepted" and wrong["text"].startswith("Anotado como error") and store.get_task("c1")["status"] == "needs_user"

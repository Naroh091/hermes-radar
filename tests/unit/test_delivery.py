from __future__ import annotations

import asyncio
from types import SimpleNamespace

from conftest import NOW, active_config, verify_destination

from radar.delivery.dispatch import dispatch
from radar.delivery.feedback import classify
from radar.delivery.notice import build_notice, decision_buttons
from radar.delivery.responses import act_on_task, resolve_reply, respond_to_proposal
from radar.scheduler import Scheduler
from radar.transports import DeliveryReceipt, TransportRegistry, telegram_send


def _seed(store, *, batch_key="now", n=2):
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="trip", title="Viaje a Lisboa", summary="Falta hotel", entities={}, expectations=[],
                      checklist=[{"item": "hotel", "status": "missing", "note": ""}], now=now, due_at="2026-11-03T06:00:00Z")
    ids = []
    for index in range(n):
        store.create_task_proposal(proposal_id=f"p{index}", task_id="t1", title=f"Propuesta {index}", reason_text="porque sí", action_class="read",
                                   operation_id="web.read", params={"message": "Tienes vuelo y no hotel."}, options=[{"id": "do", "label": "Sí"}, {"id": "no", "label": "No"}],
                                   urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint=f"fp{index}", now=now, batch_key=batch_key)
        ids.append(f"p{index}")
    return ids


def test_notice_and_digest_formatting_with_buttons(store):
    ids = _seed(store)
    task = store.get_task("t1")
    proposals = [store.get_proposal(i) for i in ids]
    notice = build_notice(task, proposals, language="es", message="Tienes vuelo y no hotel.")
    assert notice["text"].startswith("<b>✈️ Viaje a Lisboa</b>") and "Pendiente" in notice["text"] and "1. Propuesta 0" in notice["text"]
    assert notice["buttons"][0][0]["data"] == "pa:p0:do" and notice["buttons"][-2][0]["data"] == "pt:t1:done"
    assert notice["buttons"][-2][1]["data"] == "pt:t1:later" and notice["buttons"][-1][0]["data"] == "pt:t1:mute"
    assert "<b>Puedo hacer:</b>" in notice["text"] and "Responde a este mensaje" in notice["text"]
    assert notice["buttons"][0][0]["label"].startswith("1️⃣")
    assert decision_buttons(proposals[0], "en")[0]["data"] == "pa:p0:do" and decision_buttons(proposals[1], "en", index=2)[0]["label"].startswith("2️⃣")


def test_dispatch_enqueues_one_notice_per_task_and_respects_daily_limit(store):
    cfg = active_config()
    ids = _seed(store)
    assert dispatch(store, cfg, NOW) == {"notices": 0, "digests": 0, "proposals": 0}  # destination not verified yet
    verify_destination(store)
    counts = dispatch(store, cfg, NOW)
    assert counts == {"notices": 1, "digests": 0, "proposals": 2}
    assert all(store.get_proposal(i)["status"] == "sent" for i in ids)
    assert dispatch(store, cfg, NOW)["notices"] == 0
    outbox = store.list_outbox()[0]
    assert outbox["kind"] == "notice" and outbox["task_id"] == "t1" and outbox["payload"]["proposal_ids"] == ids


def test_scheduler_delivers_notice_with_buttons_and_holds_quiet_hours(store):
    cfg = active_config()
    _seed(store)
    verify_destination(store)
    sent = []
    transports = TransportRegistry()

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt("msg-1")

    transports.register("telegram", sender)
    scheduler = Scheduler(store, cfg, worker_id="w", transports=transports, clock=lambda: NOW)
    dispatch(store, cfg, NOW)
    assert asyncio.run(scheduler.tick())["delivered"] == 1
    assert sent[0]["parse_mode"] == "HTML" and sent[0]["buttons"]
    assert store.outbox_by_platform_message("msg-1")["task_id"] == "t1"
    quiet = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    _ids = store.create_task_proposal(proposal_id="late", task_id="t1", title="Tarde", reason_text="", action_class="read", operation_id=None,
                                      params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="late", now="2026-10-19T10:01:00Z", batch_key="now")
    dispatch(store, quiet, NOW)
    quiet_scheduler = Scheduler(store, quiet, worker_id="w", transports=transports, clock=lambda: NOW)
    assert asyncio.run(quiet_scheduler.tick())["delivered"] == 0
    assert [row["last_error"] for row in store.list_outbox() if row["status"] == "failed"] == ["quiet_hours"]


def test_responses_accept_decline_snooze_mute_and_checks(store):
    cfg = active_config()
    ids = _seed(store, n=3)
    accepted = respond_to_proposal(store, cfg, proposal_id=ids[0], option="do", actor="telegram:7", now_dt=NOW)
    assert accepted["outcome"] == "accepted" and store.get_proposal(ids[0])["response"]["option"] == "do"
    snoozed = respond_to_proposal(store, cfg, proposal_id=ids[1], option="later", actor="telegram:7", now_dt=NOW)
    assert snoozed["outcome"] == "snoozed" and store.get_proposal(ids[1])["snoozed_until"] == "2026-10-20T07:00:00Z"  # 09:00 Madrid, the next morning slot
    assert snoozed["text"].startswith("Te lo recuerdo mañana a las 09:00")
    assert respond_to_proposal(store, cfg, proposal_id=ids[0], option="no", actor="telegram:7", now_dt=NOW)["outcome"] == "stale"
    muted = act_on_task(store, cfg, task_id="t1", action="mute", actor="telegram:7", now_dt=NOW)
    assert muted["outcome"] == "muted" and store.get_task("t1")["muted"] == 1
    assert store.get_proposal(ids[2])["status"] == "declined" and store.get_proposal(ids[1])["status"] == "declined"
    store.create_task(task_id="c1", kind="check", title="Talla 48 vs 42", summary="", entities={}, expectations=[], checklist=[], now="2026-10-19T10:00:00Z")
    store.create_task_proposal(proposal_id="chk", task_id="c1", title="¿Es correcta la talla 48?", reason_text="", action_class="read", operation_id="check",
                               params={}, options=[{"id": "ok", "label": "Sí"}, {"id": "wrong", "label": "Error"}], urgency="high",
                               expires_at="2026-10-22T10:00:00Z", fingerprint="chk", now="2026-10-19T10:00:00Z")
    confirmed = respond_to_proposal(store, cfg, proposal_id="chk", option="ok", actor="telegram:7", now_dt=NOW)
    assert confirmed["outcome"] == "declined" and confirmed["text"].startswith("Confirmado, es correcto")
    assert store.get_task("c1")["status"] == "done"


def test_feedback_classifier_handles_negation_and_numbers():
    assert classify("sí, adelante")["intent"] == "approve"
    assert classify("no lo envíes")["intent"] == "reject"
    assert classify("adelante, pero no lo mandes")["intent"] == "unclear"
    assert classify("2")["intent"] == "approve" and classify("2")["proposal_index"] == 2
    assert classify("mejor mañana")["intent"] == "later"
    assert classify("ya lo he hecho")["intent"] == "done"
    assert classify("no me avises más de estas alertas")["intent"] == "preference"
    assert classify("¿qué has comprobado?")["intent"] == "question"
    assert classify("go ahead")["intent"] == "approve" and classify("don't send it")["intent"] == "reject"


def test_resolve_reply_routes_to_the_referenced_proposal(store):
    cfg = active_config()
    ids = _seed(store, n=2)
    verify_destination(store)
    dispatch(store, cfg, NOW)
    row = store.list_outbox()[0]
    result = resolve_reply(store, cfg, outbox_row=row, intent={"intent": "approve", "proposal_index": 2}, actor="telegram:7", now_dt=NOW, note="2")
    assert result["outcome"] == "accepted" and store.get_proposal(ids[1])["status"] == "accepted" and store.get_proposal(ids[0])["status"] == "sent"
    later = resolve_reply(store, cfg, outbox_row=row, intent={"intent": "later", "proposal_index": 0}, actor="telegram:7", now_dt=NOW)
    assert later["outcome"] == "snoozed" and store.get_proposal(ids[0])["status"] == "snoozed"


def test_numbers_point_at_what_was_shown_and_never_slide(store):
    cfg = active_config()
    ids = _seed(store, n=3)
    verify_destination(store)
    dispatch(store, cfg, NOW)
    row = {**store.list_outbox()[0], "kind": "digest", "task_id": None}  # a reminder with three numbered decisions
    assert classify("12 no") == {"intent": "reject", "proposal_index": 12} and classify("3 luego")["intent"] == "later"
    assert classify("2026")["proposal_index"] == 0 and classify("12")["intent"] == "approve"
    reply = lambda intent, index=0: resolve_reply(store, cfg, outbox_row=row, intent={"intent": intent, "proposal_index": index}, actor="telegram:7", now_dt=NOW)  # noqa: E731
    assert reply("approve", 2)["outcome"] == "accepted" and store.get_proposal(ids[1])["status"] == "accepted"
    assert reply("reject")["outcome"] == "which_one" and {store.get_proposal(pid)["status"] for pid in (ids[0], ids[2])} == {"sent"}  # a bare «no» with two open: which?
    assert reply("approve", 1)["outcome"] == "accepted" and store.get_proposal(ids[0])["status"] == "accepted"  # «1» is still the first one shown
    answered = reply("approve", 2)
    assert answered["outcome"] == "already_answered" and "Propuesta 1" in answered["text"]
    assert reply("approve", 7)["outcome"] == "unknown_number"
    assert reply("later", 3)["outcome"] == "snoozed" and store.get_proposal(ids[2])["status"] == "snoozed" and store.get_task("t1")["status"] == "snoozed"
    single = {**row, "payload": {**row["payload"], "proposal_ids": ids[:1]}}
    store.set_proposal_status(ids[2], "pending", "2026-10-19T10:05:00Z")
    assert resolve_reply(store, cfg, outbox_row={**single, "payload": {**row["payload"], "proposal_ids": ids[2:]}},
                         intent={"intent": "reject", "proposal_index": 0}, actor="telegram:7", now_dt=NOW)["outcome"] == "declined"  # one decision: no number needed


def test_telegram_send_falls_back_to_plain_text_and_sends_documents(tmp_path):
    calls = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(("message", kwargs))
            if kwargs.get("parse_mode") == "HTML":
                raise RuntimeError("Bad Request: can't parse entities")
            return SimpleNamespace(message_id=55)

        async def send_document(self, **kwargs):
            calls.append(("document", kwargs))
            return SimpleNamespace(message_id=56)

    report = tmp_path / "r.html"
    report.write_text("<html></html>")
    receipt = asyncio.run(telegram_send(Bot(), {"address": {"chat_id": "42", "thread_id": "9"}},
                                        {"text": "<b>Hola</b>", "parse_mode": "HTML", "document": {"path": str(report), "filename": "r.html"}}))
    assert receipt.platform_message_id == "55"
    assert calls[0][1]["parse_mode"] == "HTML" and calls[1][1]["text"] == "Hola" and "parse_mode" not in calls[1][1]
    assert calls[1][1]["message_thread_id"] == 9 and calls[2][0] == "document" and calls[2][1]["reply_to_message_id"] == 55


def test_radar_verify_binds_destination_to_the_owner_chat(store):
    from types import SimpleNamespace

    from radar.delivery.telegram_ui import command_text

    cfg = active_config()
    rt = SimpleNamespace(store=store, config=cfg, clock=lambda: NOW, paused=False)
    wrong = command_text(rt, "verify", identity={"platform": "telegram", "chat_id": "42", "chat_type": "private", "thread_id": "", "user_id": "999"})
    assert wrong.startswith("Actor no autorizado. Tu usuario: 999")
    ok = command_text(rt, "verify", identity={"platform": "telegram", "chat_id": "42", "chat_type": "private", "thread_id": "", "user_id": "7"})
    assert ok == "Destino de Radar verificado."
    destination = store.destination("configured:primary")
    assert destination["address"] == {"chat_id": "42"} and destination["owner_platform_id"] == "7" and destination["capabilities"]["buttons"]
    group = command_text(rt, "verify", identity={"platform": "telegram", "chat_id": "42", "chat_type": "supergroup", "thread_id": "9", "user_id": "7"})
    assert group == "Los grupos no están autorizados."


def test_notice_keeps_long_rationales_and_labels_and_translates_checklist(store):
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t9", kind="work_item", title="Apuntar DNS", summary="", entities={}, expectations=[],
                      checklist=[{"item": "deployed or verified in production", "status": "missing", "note": ""}], now=now)
    rationale = "El registro solicitado no existe y la revisión confirmó el destino. " * 6
    label = "Sí, crea el CNAME con el proxy desactivado"
    store.create_task_proposal(proposal_id="p9", task_id="t9", title="Autorizar la creación del CNAME", reason_text=rationale, action_class="external",
                               operation_id=None, params={}, options=[{"id": "ok", "label": label}, {"id": "no", "label": "No"}], urgency="normal",
                               expires_at="2026-10-22T10:00:00Z", fingerprint="fp9", now=now)
    payload = build_notice(store.get_task("t9"), [store.get_proposal("p9")], language="es")
    assert rationale.strip() in payload["text"] and "desplegado o verificado en producción" in payload["text"]
    assert payload["buttons"][0][0]["label"] == label


def test_button_feedback_says_whether_the_approved_step_runs(store):
    from radar.delivery.telegram_ui import _after_accept, with_followup

    cfg = active_config()
    ids = _seed(store, n=2)
    rt = SimpleNamespace(store=store, config=cfg, clock=lambda: NOW)
    first = respond_to_proposal(store, cfg, proposal_id=ids[0], option="do", actor="telegram:7", now_dt=NOW)
    text = with_followup(first, _after_accept(rt, first, "do"))["text"]
    assert text.startswith("Autorizado: Propuesta 0.") and "En marcha" in text
    assert store.get_proposal(ids[0])["status"] == "executing" and len(store.list_jobs()) == 1
    second = respond_to_proposal(store, cfg, proposal_id=ids[1], option="do", actor="telegram:7", now_dt=NOW)
    text = with_followup(second, _after_accept(rt, second, "do"))["text"]
    assert "Todavía no se ha ejecutado" in text and "otro trabajo en curso" in text and store.get_proposal(ids[1])["status"] == "waiting"
    again = respond_to_proposal(store, cfg, proposal_id=ids[1], option="do", actor="telegram:7", now_dt=NOW)
    assert again["outcome"] == "stale" and "en espera" in again["text"]
    cancelled = respond_to_proposal(store, cfg, proposal_id=ids[1], option="no", actor="telegram:7", now_dt=NOW)
    assert cancelled["outcome"] == "declined" and store.get_proposal(ids[1])["status"] == "declined"


def test_notice_shrinks_long_parts_at_word_boundaries_to_fit_telegram(store):
    now = "2026-10-19T10:00:00Z"
    long = ("palabra " * 120).strip()
    store.create_task(task_id="t8", kind="trip", title="Viaje largo", summary=long, entities={}, expectations=[], checklist=[], now=now)
    ids = []
    for index in range(4):
        store.create_task_proposal(proposal_id=f"q{index}", task_id="t8", title=f"Paso {index}", reason_text=long, action_class="read", operation_id="web.read",
                                   params={"draft": long * 2, "message": long}, options=[{"id": "do", "label": "Sí"}, {"id": "no", "label": "No"}],
                                   urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint=f"q{index}", now=now)
        ids.append(f"q{index}")
    payload = build_notice(store.get_task("t8"), [store.get_proposal(i) for i in ids], language="es")
    text = payload["text"]
    assert len(text) <= 3800 and "palabra…" in text and "palabr…" not in text and len(payload["buttons"]) == 6


def test_owner_followups_bypass_the_daily_notice_limit(store):
    cfg = active_config(notifications={"daily_limit": 1})
    verify_destination(store)
    _seed(store, n=1)
    now = "2026-10-19T10:00:00Z"
    for task_id, followup in (("t2", True), ("t3", False)):
        store.create_task(task_id=task_id, kind="work_item", title=f"Tarea {task_id}", summary="", entities={}, expectations=[], checklist=[], now=now)
        store.create_task_proposal(proposal_id=f"p-{task_id}", task_id=task_id, title="Siguiente paso", reason_text="", action_class="read", operation_id="web.read",
                                   params={"owner_followup": True} if followup else {}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z",
                                   fingerprint=f"fp-{task_id}", now=now, batch_key="now")
    counts = dispatch(store, cfg, NOW)
    assert counts["notices"] == 2  # one capped notice plus the answer to an approval; the other task waits for tomorrow
    assert store.get_proposal("p-t2")["status"] == "sent"
    assert sum(1 for pid in ("p-t3", "p0") if store.get_proposal(pid)["status"] == "sent") == 1
    assert store.notification_count_since("2026-10-19T00:00:00Z") == 0  # nothing delivered yet; the cap counts sent rows only


def test_only_the_first_option_executes_and_review_shows_the_draft(store):
    from radar.delivery.responses import option_intent

    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t5", kind="work_item", title="Bug de Bernd", summary="Faltan capturas", entities={}, expectations=[], checklist=[], now=now)
    options = [{"id": "do_it", "label": "Enviar borrador"}, {"id": "review_first", "label": "Revisarlo primero"}, {"id": "no_enviar", "label": "No enviar"}]
    store.create_task_proposal(proposal_id="p5", task_id="t5", title="Enviar la solicitud a Bernd", reason_text="Listo", action_class="external", operation_id="gmail.reply",
                               params={"draft": "Hi Bernd,\n\nCould you send screenshots?", "draft_ref": "Draft ID: r123"}, options=options, urgency="normal",
                               expires_at="2026-10-22T10:00:00Z", fingerprint="fp5", now=now)
    proposal = store.get_proposal("p5")
    assert [option_intent(proposal, o) for o in ("do_it", "review_first", "no_enviar", "later", "zzz")] == ["accept", "review", "decline", "later", "review"]
    review = respond_to_proposal(store, cfg, proposal_id="p5", option="review_first", actor="telegram:7", now_dt=NOW)
    assert review["outcome"] == "review" and "Could you send screenshots?" in review["detail"] and "Enviar borrador" in review["detail"] and "r123" in review["detail"]
    assert store.get_proposal("p5")["status"] == "pending"  # still open after reviewing
    assert respond_to_proposal(store, cfg, proposal_id="p5", option="no_enviar", actor="telegram:7", now_dt=NOW)["outcome"] == "declined"
    # A sent detail message is recorded so that replying to it resolves the proposal.
    verify_destination(store)
    store.record_sent(outbox_id="o5", destination_id="configured:primary", notification_key="detail:p5", payload={"text": "x", "proposal_ids": ["p5"], "task_id": "t5"},
                      platform_message_id="777", now=now, task_id="t5", kind="detail")
    row = store.outbox_by_platform_message("777")
    assert row["kind"] == "detail" and row["payload"]["proposal_ids"] == ["p5"]


def _thread(store, task_id="t7", *, account="me@example.org"):
    now = "2026-10-19T10:00:00Z"
    store.add_source(source_id="src", kind="gmail", account_ref=account, resource_scope={}, retention_days=30, now=now)
    store.create_task(task_id=task_id, kind="work_item", title="Bug de subida de fotos", summary="Bernd reporta un fallo", entities={}, expectations=[], checklist=[], now=now)
    messages = [
        ("m1", "2026-10-18T15:30:00Z", "Bug uploading photos", "Bernd Müller <bernd@example.com>", "Hi David, the upload fails with a 500 when the file is larger than 8 MB. Logs attached."),
        ("m2", "2026-10-18T16:10:00Z", "Re: Bug uploading photos", "David <me@example.org>", "Thanks Bernd, looking into it."),
        ("m3", "2026-10-19T09:02:00Z", "Re: Bug uploading photos", "Bernd Müller <bernd@example.com>",
         "It still happens today with a 9 MB file. The browser shows a blank page. On Sat, Oct 18, 2026 at 6:10 PM David <me@example.org> wrote: Thanks Bernd, looking into it."),
    ]
    for ref, at, subject, sender, body in messages:
        store.add_signal(signal_id=f"sig-{ref}", source_id="src", kind="email", occurred_at=at, external_ref=ref, group_key="thread:x",
                         title=subject, snippet=body, meta={"sender": sender, "account": account, "thread_id": "th1"}, fingerprint=f"f{ref}", now=now)
        store.link_task_signal(task_id, f"sig-{ref}", "origin" if ref == "m1" else "advances", now)


def test_notice_shows_who_wrote_when_and_what_in_the_email_thread(store):
    from radar.delivery.context import strip_quoted, thread_context, when

    cfg = active_config()
    cfg["owner"] = {**cfg.get("owner", {}), "language": "es", "timezone": "Europe/Madrid"}
    _thread(store)
    context = thread_context(store, "t7", cfg, now_dt=NOW)
    assert context["sender"] == "Bernd Müller" and context["when"] == "19 oct 11:02" and context["count"] == 3
    assert context["others"] == ["tú"] and context["subject"] == "Bug uploading photos" and context["account"] == ""
    assert context["excerpt"] == "It still happens today with a 9 MB file. The browser shows a blank page."
    store.create_task_proposal(proposal_id="p7", task_id="t7", title="Pedir el fichero de 9 MB", reason_text="Para reproducirlo", action_class="local",
                               operation_id="gmail.create_draft", params={"message": "Bernd sigue viendo el fallo."}, options=[{"id": "do", "label": "Sí"}, {"id": "no", "label": "No"}],
                               urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fp7", now="2026-10-19T10:00:00Z")
    payload = build_notice(store.get_task("t7"), [store.get_proposal("p7")], language="es", context=context)
    text = payload["text"]
    assert "<b>Correo:</b> Bernd Müller · 19 oct 11:02 · 3 mensajes en el hilo · con tú" in text
    assert "<i>Asunto: Bug uploading photos</i>" in text and "<blockquote>It still happens today" in text and "wrote:" not in text
    assert text.index("Bernd sigue viendo") < text.index("<b>Correo:</b>") < text.index("<b>Puedo hacer:</b>")
    # The account is named only when the owner watches several mailboxes.
    cfg["sources"] = [{"id": "a", "kind": "gmail"}, {"id": "b", "kind": "gmail"}]
    context = thread_context(store, "t7", cfg, now_dt=NOW)
    assert context["account"] == "me@example.org"
    # Helpers: quoted history is stripped, bodies without a marker stay whole, years appear only when not current.
    assert strip_quoted("Short reply. El 18 oct 2026, David escribió: hola") == "Short reply."
    assert strip_quoted("On Sat, Oct 18 David wrote: only a quote") == "On Sat, Oct 18 David wrote: only a quote"
    assert when("2025-12-24T18:00:00Z", language="en", tz="Europe/Madrid", now_dt=NOW) == "24 Dec 19:00 2025"
    assert when("not a date") == "" and when("2026-10-19T09:02:00Z", tz="Nowhere/Invalid", now_dt=NOW) == "19 oct 09:02"


def test_email_context_shrinks_before_the_notice_is_cut_and_review_quotes_the_thread(store):
    from radar.delivery.context import thread_context
    from radar.delivery.notice import MAX_MESSAGE

    cfg = active_config()
    _thread(store, "t8b")
    ids = []
    for index in range(4):
        store.create_task_proposal(proposal_id=f"p8b{index}", task_id="t8b", title=f"Paso {index} " + "detalle " * 75, reason_text="Razón larga. " * 60, action_class="read",
                                   operation_id="web.read", params={"message": "Situación. " * 80, "draft": "Línea del borrador. " * 70}, options=[{"id": "do", "label": "Sí"}, {"id": "no", "label": "No"}],
                                   urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint=f"fp8b{index}", now="2026-10-19T10:00:00Z")
        ids.append(f"p8b{index}")
    context = thread_context(store, "t8b", cfg, now_dt=NOW)
    payload = build_notice(store.get_task("t8b"), [store.get_proposal(i) for i in ids], language="es", context=context)
    assert len(payload["text"]) <= MAX_MESSAGE and "<b>Correo:</b> Bernd Müller" in payload["text"]  # header survives, excerpt is the first to go
    assert "<blockquote>" not in payload["text"] and payload["text"].count("<b>Puedo hacer:</b>") == 1
    # "Revisarlo primero" starts with what is being answered.
    store.create_task_proposal(proposal_id="p8r", task_id="t8b", title="Responder a Bernd", reason_text="Listo", action_class="external", operation_id="gmail.reply",
                               params={"draft": "Hi Bernd, could you share the 9 MB file?", "draft_ref": "Draft ID: r9"},
                               options=[{"id": "send", "label": "Enviar"}, {"id": "review", "label": "Revisar"}, {"id": "no", "label": "No"}],
                               urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fp8r", now="2026-10-19T10:00:00Z")
    review = respond_to_proposal(store, cfg, proposal_id="p8r", option="review", actor="telegram:7", now_dt=NOW)
    detail = review["detail"]
    assert detail.startswith("En respuesta a Bernd Müller (19 oct") and "«It still happens today" in detail and "wrote:" not in detail
    assert detail.index("It still happens") < detail.index("Hi Bernd, could you share")


def test_work_item_result_notices_attach_the_html_report(store, tmp_path, monkeypatch):
    from radar.delivery import dispatch as dispatch_module
    from radar.delivery.dispatch import notify_job_result

    cfg = active_config()
    verify_destination(store)
    monkeypatch.setattr(dispatch_module, "get_hermes_home", lambda: tmp_path)
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="w1", kind="work_item", title="Fix upload", summary="500 on big files", entities={}, expectations=[], checklist=[], now=now)
    signal = {"id": "j1", "title": "[succeeded] Reproducir", "snippet": "Found the limit", "meta": {"status": "succeeded", "findings": ["nginx 8m"], "effects": ["pushed radar/x"], "refs": []}}
    assert notify_job_result(store, cfg, task_id="w1", signal=signal, now_dt=NOW) is True
    row = store.list_outbox(limit=1)[0]
    document = row["payload"].get("document")
    assert document and document["filename"] == "informe.html" and document["path"].startswith(str(tmp_path)) and "Fix upload" in open(document["path"], encoding="utf-8").read()
    # Trips and failed steps carry no report.
    store.create_task(task_id="w2", kind="trip", title="Viaje", summary="", entities={}, expectations=[], checklist=[], now=now)
    assert notify_job_result(store, cfg, task_id="w2", signal={**signal, "id": "j2"}, now_dt=NOW) is True
    trip_row = next(r for r in store.list_outbox(limit=5) if r.get("task_id") == "w2")
    assert "document" not in trip_row["payload"]

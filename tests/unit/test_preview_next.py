from __future__ import annotations

from types import SimpleNamespace

from conftest import NOW, active_config, verify_destination

from radar.delivery.dispatch import dispatch
from radar.delivery.preview import format_next, preview_next


def _seed(store, *, batch_key="now", proposal_id="p0", operation_id="web.read",
          action_class="read", urgency="normal", params=None):
    now = "2026-10-19T10:00:00Z"
    try:
        store.create_task(task_id="t1", kind="trip", title="Viaje a Lisboa", summary="Falta hotel",
                          entities={}, expectations=[], checklist=[], now=now)
    except Exception:
        pass
    store.create_task_proposal(
        proposal_id=proposal_id, task_id="t1", title="Reservar hotel", reason_text="No hay reserva",
        action_class=action_class, operation_id=operation_id,
        params=params if params is not None else {"message": "Tienes vuelo y no hotel."},
        options=[{"id": "do", "label": "Sí"}, {"id": "no", "label": "No"}],
        urgency=urgency, expires_at="2026-10-22T10:00:00Z",
        fingerprint=f"fp-{proposal_id}", now=now, batch_key=batch_key)


def _rt(store, cfg):
    return SimpleNamespace(store=store, config=cfg, clock=lambda: NOW, paused=False)


def test_next_empty_when_nothing_waits(store):
    cfg = active_config()
    verify_destination(store)
    preview = preview_next(store, cfg, NOW)
    assert preview["kind"] == "empty"
    assert format_next(preview, cfg, NOW).startswith("No hay avisos pendientes")


def test_next_shows_ready_notice_and_ignores_quiet_hours(store):
    cfg = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    verify_destination(store)
    _seed(store)  # NOW is 12:00 in Madrid: inside quiet hours, nothing enqueued yet
    preview = preview_next(store, cfg, NOW)
    assert preview["kind"] == "notice" and preview["held"] == "quiet_hours"
    assert "Viaje a Lisboa" in preview["text"] and preview["when"] is not None
    text = format_next(preview, cfg, NOW)
    assert text.startswith("📬 Siguiente aviso") and "silencio" in text and "13:00" in text


def test_next_urgent_notice_crosses_quiet_hours(store):
    cfg = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    verify_destination(store)
    _seed(store, proposal_id="urg", operation_id="check", urgency="high",
          params={"message": "Pago bloqueado.", "urgency": "now"})
    preview = preview_next(store, cfg, NOW)
    assert preview["kind"] == "notice" and preview["urgent"] is True and preview["held"] is None
    assert "cruza las horas de silencio" in format_next(preview, cfg, NOW)


def test_next_queued_outbox_wins_over_pending(store):
    cfg = active_config()
    verify_destination(store)
    _seed(store)
    assert dispatch(store, cfg, NOW)["notices"] == 1
    quiet = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    preview = preview_next(store, quiet, NOW)  # the enqueued notice is held by quiet hours now
    assert preview["kind"] == "queued" and preview["held"] == "quiet_hours"
    assert "Viaje a Lisboa" in preview["text"]
    assert format_next(preview, quiet, NOW).startswith("📬 Siguiente aviso (en cola")


def test_next_reminder_for_digest_decisions(store):
    cfg = active_config()
    verify_destination(store)
    _seed(store, batch_key="digest", proposal_id="dg", operation_id="ask")
    preview = preview_next(store, cfg, NOW)
    assert preview["kind"] == "reminder" and preview["when"] is not None
    text = format_next(preview, cfg, NOW)
    assert text.startswith("⏰ Siguiente recordatorio") and "Viaje a Lisboa" in text


def test_next_command_launches_and_answers(store):
    from radar.delivery.telegram_ui import command_text

    cfg = active_config()
    verify_destination(store)
    _seed(store)
    answer = command_text(_rt(store, cfg), "next")
    assert answer.startswith("📬 Lanzado") and "Viaje a Lisboa" in answer
    row = store.list_outbox(limit=1)[0]
    assert row["kind"] == "notice" and row["payload"]["urgent"] is True
    assert "status | list | next" in command_text(_rt(store, cfg), "bogus")


def test_next_inactive_without_destination(store):
    cfg = active_config()
    preview = preview_next(store, cfg, NOW)
    assert preview["kind"] == "inactive" and preview["reason"] == "no_destination"
    assert "verify" in format_next(preview, cfg, NOW)


def test_next_launches_queued_message_held_by_quiet_hours(store):
    from radar.delivery.preview import deliver_next, format_sent

    cfg = active_config()
    verify_destination(store)
    _seed(store)
    assert dispatch(store, cfg, NOW)["notices"] == 1
    quiet = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    result = deliver_next(store, quiet, NOW)
    assert result["kind"] == "queued" and result["quiet"] is True
    row = store.get_outbox(result["outbox_id"])
    assert row["payload"]["urgent"] is True and row["last_error"] is None
    assert row["next_attempt_at"] <= "2026-10-19T10:00:00Z"
    assert "aunque haya silencio" in format_sent(result, quiet)


def test_next_launches_ready_notice_and_tick_delivers_it_in_quiet_hours(store):
    import asyncio

    from radar.delivery.preview import deliver_next
    from radar.scheduler import Scheduler
    from radar.transports import DeliveryReceipt, TransportRegistry

    cfg = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    verify_destination(store)
    _seed(store)
    result = deliver_next(store, cfg, NOW)
    assert result["kind"] == "notice" and result["title"] == "Viaje a Lisboa"
    sent = []
    transports = TransportRegistry()

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt("msg-9")

    transports.register("telegram", sender)
    scheduler = Scheduler(store, cfg, worker_id="w", transports=transports, clock=lambda: NOW)
    assert asyncio.run(scheduler.tick())["delivered"] == 1
    assert len(sent) == 1 and "Viaje a Lisboa" in sent[0]["text"]


def test_next_launches_digest_reminder(store):
    from radar.delivery.preview import deliver_next

    cfg = active_config()
    verify_destination(store)
    _seed(store, batch_key="digest", proposal_id="dg", operation_id="ask")
    result = deliver_next(store, cfg, NOW)
    assert result["kind"] == "reminder"
    row = store.get_outbox(result["outbox_id"])
    assert row["kind"] == "digest" and row["payload"]["urgent"] is True
    assert store.get_proposal("dg")["status"] == "sent"


def test_next_empty_launches_nothing(store):
    from radar.delivery.preview import deliver_next, format_sent

    cfg = active_config()
    verify_destination(store)
    assert deliver_next(store, cfg, NOW)["kind"] == "empty"
    assert format_sent({"kind": "empty"}, cfg).startswith("No hay avisos pendientes")
    assert store.list_outbox() == []

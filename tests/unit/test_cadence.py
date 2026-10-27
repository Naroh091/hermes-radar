"""Urgency decides when news reaches the owner: now, the next summary slot, or the morning summary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from conftest import NOW, active_config, config, extract_answer, plan_answer, verify_destination

from radar.delivery.cadence import digest_due, latest_moment, next_delivery, next_paced, waking_window
from radar.delivery.dispatch import _urgent, dispatch, iso, next_reminder_at, select_reminder
from radar.pipeline import act, extract as extract_step, reason


def test_summary_slots_follow_the_owners_clock():
    cfg = active_config()  # Europe/Madrid, slots 09:00 / 14:00 / 19:00; NOW is 12:00 local
    assert next_delivery(NOW, "now", cfg) is None
    assert next_delivery(NOW, "today", cfg) == datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    assert next_delivery(NOW, "this_week", cfg) == datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc)
    assert next_delivery(NOW, "whenever", cfg) == datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc)
    late = datetime(2026, 10, 19, 18, 30, tzinfo=timezone.utc)  # 20:30 local: nothing left today
    assert next_delivery(late, "today", cfg) == datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc)
    early = datetime(2026, 10, 19, 5, 0, tzinfo=timezone.utc)  # 07:00 local: the morning slot is still ahead
    assert next_delivery(early, "whenever", cfg) == datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)
    assert digest_due(NOW, None, cfg) is True
    assert digest_due(NOW, NOW - timedelta(minutes=30), cfg) is False  # 09:00 already served, 14:00 not yet
    assert digest_due(datetime(2026, 10, 19, 12, 1, tzinfo=timezone.utc), NOW - timedelta(minutes=30), cfg) is True
    paced_cfg = active_config(notifications={"digest_times": []})  # no slots: reminders paced over the day
    assert next_delivery(NOW, "whenever", paced_cfg) is None and next_delivery(NOW, "today", paced_cfg) is None


def _apply(store, cfg, plan):
    return act.apply(store, cfg, signals=[], extract=extract_step.validate_extract(extract_answer()), plan=reason.validate_plan(plan), now_dt=NOW, preferences=[])


def test_only_what_cannot_wait_interrupts_and_the_rest_waits_for_its_slot(store):
    cfg = config()
    # A decision (the owner's) and a scheduled piece of work (the assistant's own).
    hotel_ask = {"title": "Buscar hotel en Lisboa 3-5 nov", "rationale": "No hay reserva", "action_class": "read", "operation": "ask", "when": "", "urgency": "normal",
                 "options": [{"id": "si", "label": "Sí, busca"}, {"id": "no", "label": "No"}]}
    steps = [hotel_ask, plan_answer()["proposals"][1]]
    weekly = _apply(store, cfg, plan_answer(proposals=steps, notify={"disposition": "notify_now", "urgency": "this_week", "importance": "normal", "reason": "x"}))
    assert weekly.disposition == "digest"
    proposals = store.list_proposals(task_id=weekly.task_id)
    hotel = next(p for p in proposals if p["title"].startswith("Buscar hotel"))
    checkin = next(p for p in proposals if p["title"].startswith("Hacer check-in"))
    assert hotel["batch_key"] == "digest" and hotel["params"]["deliver_after"] == "2026-10-20T07:00:00Z" and hotel["params"]["urgency"] == "this_week"
    assert checkin["batch_key"] == "auto" and checkin["params"]["deliver_after"] == "2026-11-02T06:00:00Z"  # its own moment, run by itself when it comes
    urgent = _apply(store, cfg, plan_answer(task={**plan_answer()["task"], "title": "Vuelo mañana sin hotel"}, proposals=steps,
                                            notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}))
    assert urgent.disposition == "notify_now"
    now_items = store.list_proposals(task_id=urgent.task_id)
    asked = next(p for p in now_items if p["title"].startswith("Buscar"))
    assert asked["batch_key"] == "now" and asked["params"]["deliver_after"] == ""
    today = _apply(store, cfg, plan_answer(task={**plan_answer()["task"], "title": "Vuelo en dos días"}, proposals=steps,
                                           notify={"disposition": "digest", "urgency": "today", "importance": "normal", "reason": "x"}))
    assert next(p for p in store.list_proposals(task_id=today.task_id) if p["title"].startswith("Buscar"))["params"]["deliver_after"] == "2026-10-19T12:00:00Z"
    # A plan written before urgency existed still validates: notify_now means now, digest means today.
    legacy = reason.validate_plan(plan_answer(notify={"disposition": "digest", "reason": "x"}))
    assert legacy["notify"]["urgency"] == "today" and legacy["notify"]["importance"] == "normal"


def test_the_digest_goes_out_at_its_slot_and_quiet_hours_yield_only_to_real_urgency(store):
    cfg = active_config()
    verify_destination(store)
    def proposal(pid, deliver_after="", urgency="normal", params_urgency="today"):  # a decision: only those travel in a reminder
        # one task per proposal: the per-task reminder cooldown must not hide the slot timing this test checks
        store.create_task(task_id=f"t-{pid}", kind="trip", title="Viaje", summary="", entities={}, expectations=[], checklist=[], now="2026-10-19T10:00:00Z")
        store.create_task_proposal(proposal_id=pid, task_id=f"t-{pid}", title=f"¿Paso {pid}?", reason_text="", action_class="read", operation_id="ask",
                                   params={"deliver_after": deliver_after, "urgency": params_urgency}, options=[{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}],
                                   urgency=urgency, expires_at="2026-10-22T10:00:00Z", fingerprint=pid, now="2026-10-19T10:00:00Z", batch_key="digest")

    proposal("p1")
    assert dispatch(store, cfg, NOW)["digests"] == 1  # first digest ever: the morning slot is owed
    proposal("p2")
    assert dispatch(store, cfg, NOW + timedelta(minutes=40))["digests"] == 0  # 12:40 local: no slot passed since
    proposal("p3", deliver_after="2026-10-20T07:00:00Z")  # waits for tomorrow's morning
    counts = dispatch(store, cfg, datetime(2026, 10, 19, 12, 1, tzinfo=timezone.utc))  # 14:01 local
    assert counts["digests"] == 1 and store.get_proposal("p2")["status"] == "sent" and store.get_proposal("p3")["status"] == "pending"
    assert _urgent({"urgency": "high", "params": {"urgency": "today"}}) is False
    assert _urgent({"urgency": "high", "params": {"urgency": "now"}}) is True and _urgent({"urgency": "high", "params": {}}) is True
    assert _urgent({"urgency": "normal", "params": {"urgency": "now"}}) is False


# -- paced reminders (digest_times: []) ------------------------------------------------------------------

MADRID = ZoneInfo("Europe/Madrid")


def _paced(**notifications):
    return active_config(notifications={"digest_times": [], "quiet_start": "22:00", "quiet_end": "08:00", "reminders_per_day": 5,
                                        "reminder_gap_minutes": 60, "reminder_lead_minutes": 180, **notifications})


def _local(day, hour, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=MADRID)


def _decision(store, pid, title, *, at="2026-10-19T06:00:00Z", due_at=None, urgency="normal", plan_urgency="this_week"):
    store.create_task(task_id=f"t-{pid}", kind="work_item", title=title, summary="", entities={}, expectations=[], checklist=[], now=at, due_at=due_at)
    store.create_task_proposal(proposal_id=pid, task_id=f"t-{pid}", title=f"¿{title}?", reason_text="", action_class="read", operation_id="ask",
                               params={"urgency": plan_urgency}, options=[{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}],
                               urgency=urgency, expires_at="2026-10-30T10:00:00Z", fingerprint=pid, now=at, batch_key="digest")


def _sent(store, at, kind="digest"):
    store.enqueue_notification(outbox_id=f"o-{at.isoformat()}", destination_id="configured:primary", followup_id=None, proposal_id=None,
                               notification_key=f"{kind}:{at.isoformat()}", payload={"text": "x"}, now=iso(at), kind=kind)


def test_the_waking_day_runs_from_the_end_of_quiet_hours_to_their_start():
    cfg = _paced()
    assert waking_window(_local(19, 10), cfg) == (_local(19, 8), _local(19, 22))
    assert waking_window(_local(19, 23), cfg) == (_local(20, 8), _local(20, 22))  # quiet now: the next one
    late = _paced(quiet_start="00:00", quiet_end="08:00")
    assert waking_window(_local(19, 23, 30), late) == (_local(19, 8), _local(20, 0))
    # The night the clocks go back (25/10) the window keeps its clock times.
    assert waking_window(_local(25, 9), cfg) == (_local(25, 8), _local(25, 22))
    assert latest_moment(_local(20, 8, 30), cfg) == _local(19, 21, 45)  # 05:30 is quiet: the evening before


def test_reminders_spread_over_what_is_left_of_the_day_as_many_as_wait():
    cfg = _paced()
    yesterday = _local(18, 20)
    assert next_paced(_local(19, 8, 1), cfg, last_contact=yesterday, reminders_in_window=0, waiting=3) == _local(19, 8)  # the first, at once
    # One went at 08:00; the three still waiting share the 14 hours left: every 3 h 30.
    assert next_paced(_local(19, 8, 1), cfg, last_contact=_local(19, 8), reminders_in_window=1, waiting=3) == _local(19, 11, 30)
    # Twenty waiting are still five a day, never closer than the gap.
    assert next_paced(_local(19, 8, 1), cfg, last_contact=_local(19, 8), reminders_in_window=1, waiting=20) == _local(19, 10, 48)
    tight = _paced(reminders_per_day=50)
    assert next_paced(_local(19, 8, 1), tight, last_contact=_local(19, 8), reminders_in_window=1, waiting=50) == _local(19, 9)
    # Without a gap, a long queue late in the day is still one at a time, not a burst before quiet hours.
    loose = _paced(reminders_per_day=20, reminder_gap_minutes=0)
    assert next_paced(_local(19, 21, 50), loose, last_contact=_local(19, 21, 45), reminders_in_window=10, waiting=20) == _local(19, 21, 52, 30)
    assert next_paced(_local(19, 8, 1), loose, last_contact=_local(19, 8), reminders_in_window=1, waiting=2) == _local(19, 12, 40)
    # The day's five are spent: tomorrow morning.
    assert next_paced(_local(19, 15), cfg, last_contact=_local(19, 14), reminders_in_window=5, waiting=4) == _local(20, 8)
    assert next_paced(_local(19, 23), cfg, last_contact=_local(19, 21), reminders_in_window=4, waiting=3) == _local(20, 8)  # quiet hours


def test_a_decision_for_tonight_goes_out_early_enough_and_first(store):
    cfg = _paced()
    verify_destination(store)
    _decision(store, "old", "Elegir proveedor", at="2026-10-18T06:00:00Z")
    _decision(store, "dinner", "Confirmar la cena de las 20:00", at="2026-10-19T07:00:00Z", due_at=iso(_local(19, 20)))
    _decision(store, "later", "Revisar el contrato", at="2026-10-18T07:00:00Z")
    for hour, minute in ((8, 0), (10, 0), (12, 0), (13, 30)):
        _sent(store, _local(19, hour, minute))
    # Four reminders today, one left for the day: the regular one would be at 17:45, too late for an answer by 17:00.
    assert next_reminder_at(store, cfg, _local(19, 14)) == _local(19, 17, 45)
    assert select_reminder(store, cfg, _local(19, 14)) is None  # the gap since 13:30 is not over
    chosen = select_reminder(store, cfg, _local(19, 14, 31))
    assert [p["id"] for p in chosen["decisions"]] == ["dinner"]
    # The day's five are spent: the rest waits for tomorrow, the oldest first.
    store.mark_proposals_sent(["dinner"], "o-dinner", iso(_local(19, 14, 31)))
    _sent(store, _local(19, 14, 31))
    assert select_reminder(store, cfg, _local(19, 16)) is None
    assert next_reminder_at(store, cfg, _local(19, 16)) == _local(20, 8)
    assert [p["id"] for p in select_reminder(store, cfg, _local(20, 8))["decisions"]] == ["old"]


def test_a_task_with_an_unanswered_reminder_is_held_for_its_cooldown(store):
    cfg = _paced(reminder_gap_minutes=0)
    verify_destination(store)
    # tun was reminded 15 minutes ago and the owner has not answered; a fresh high-urgency variant waits behind it.
    store.create_task(task_id="tun", kind="work_item", title="Actualizar cloudflared", summary="", entities={}, expectations=[], checklist=[], now="2026-10-19T06:00:00Z")
    opts = [{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}]
    store.create_task_proposal(proposal_id="tun-1", task_id="tun", title="¿Opera un túnel?", reason_text="", action_class="read", operation_id="ask",
                               params={"urgency": "this_week"}, options=opts, urgency="high", expires_at="2026-10-30T10:00:00Z", fingerprint="tun-1",
                               now="2026-10-19T06:00:00Z", batch_key="digest")
    store.enqueue_notification(outbox_id="o-tun", destination_id="configured:primary", followup_id=None, proposal_id=None, notification_key="digest:tun",
                               payload={"text": "x", "proposal_ids": ["tun-1"]}, now=iso(_local(19, 9, 45)), task_id="tun", kind="digest")
    store.mark_proposals_sent(["tun-1"], "o-tun", iso(_local(19, 9, 45)))
    store.create_task_proposal(proposal_id="tun-2", task_id="tun", title="¿Debe mantener activo?", reason_text="", action_class="read", operation_id="ask",
                               params={"urgency": "this_week"}, options=opts, urgency="high", expires_at="2026-10-30T10:00:00Z", fingerprint="tun-2",
                               now=iso(_local(19, 9, 50)), batch_key="digest")
    _decision(store, "other", "Otra cosa", at="2026-10-19T06:00:00Z", urgency="high")
    # At 10:00 both tun-2 and other press, but tun still owes an answer: it is held, so the other task goes.
    assert [p["id"] for p in select_reminder(store, cfg, _local(19, 10))["decisions"]] == ["other"]


def test_paced_reminders_keep_quiet_hours_and_high_urgency_only_waits_for_the_gap(store):
    cfg = _paced()
    verify_destination(store)
    _decision(store, "a", "Elegir proveedor")
    _decision(store, "b", "Revisar el contrato")
    _sent(store, _local(19, 8))
    assert select_reminder(store, cfg, _local(19, 23)) is None  # quiet hours
    assert select_reminder(store, cfg, _local(19, 9, 30)) is None  # the next regular one is at 12:40
    for hour in (10, 12, 14, 16):
        _sent(store, _local(19, hour))
    # The day's five are spent; «today» was the 18th, so a step proposed then presses no more.
    _decision(store, "stale", "Contestar a Ana hoy", at="2026-10-18T07:00:00Z", plan_urgency="today")
    assert select_reminder(store, cfg, _local(19, 19, 30)) is None
    _decision(store, "leak", "Rotar la clave expuesta", at="2026-10-19T17:20:00Z", urgency="high")
    assert [p["id"] for p in select_reminder(store, cfg, _local(19, 19, 30))["decisions"]] == ["leak"]

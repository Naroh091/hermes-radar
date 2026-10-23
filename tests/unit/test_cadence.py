"""Urgency decides when news reaches the owner: now, the next summary slot, or the morning summary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import NOW, active_config, config, extract_answer, plan_answer, verify_destination

from radar.delivery.cadence import digest_due, next_delivery
from radar.delivery.dispatch import _urgent, dispatch
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
    plain = active_config(notifications={"digest_times": []})  # no slots: the old batch cadence
    assert next_delivery(NOW, "whenever", plain) is None
    assert digest_due(NOW, NOW - timedelta(minutes=10), plain) is False and digest_due(NOW, NOW - timedelta(minutes=31), plain) is True


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
    store.create_task(task_id="t1", kind="trip", title="Viaje", summary="", entities={}, expectations=[], checklist=[], now="2026-10-19T10:00:00Z")

    def proposal(pid, deliver_after="", urgency="normal", params_urgency="today"):  # a decision: only those travel in a reminder
        store.create_task_proposal(proposal_id=pid, task_id="t1", title=f"¿Paso {pid}?", reason_text="", action_class="read", operation_id="ask",
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

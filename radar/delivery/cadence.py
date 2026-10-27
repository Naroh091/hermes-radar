"""When a non-urgent notice should reach the owner.

The planner judges urgency; code turns it into a delivery time. By default the reminders are paced over the waking
day (from ``quiet_end`` to ``quiet_start``) instead of fixed slots: as many as there are decisions waiting, up to
``notifications.reminders_per_day``, spread evenly over what is left of the day (and never closer than
``reminder_gap_minutes`` when set); a decision whose moment is near (its task's ``due_at`` or a plan urgency of ``today``, minus
``reminder_lead_minutes``) or of high urgency goes out at once (after the gap, when one is set). With ``digest_times`` set
(local HH:MM) the old fixed slots apply: ``today`` takes the next slot of the day, ``this_week`` and ``whenever``
wait for the first slot of a day.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

URGENCIES = ("now", "today", "this_week", "whenever")
DEFAULT_TIMES: list[str] = []  # paced; until 2026-10-25 the slots were 09:00, 14:00 and 19:00
REMINDERS_PER_DAY = 20
LEAD_MINUTES = 180
MIN_GAP_MINUTES = 0  # no floor by default: the even spread is the pace; set it to keep a minimum between messages
EDGE = timedelta(minutes=15)  # a moment that falls in quiet hours moves to this long before they start


def _tz(config: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(str((config.get("owner") or {}).get("timezone") or "UTC"))
    except Exception:
        return ZoneInfo("UTC")


def digest_times(config: dict[str, Any]) -> list[time]:
    raw = (config.get("notifications") or {}).get("digest_times")
    if raw is None:
        raw = DEFAULT_TIMES
    slots: list[time] = []
    for item in raw if isinstance(raw, list) else []:
        text = str(item or "").strip()
        if len(text) == 5 and text[2] == ":":
            try:
                hour, minute = int(text[:2]), int(text[3:])
            except ValueError:
                continue
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                slots.append(time(hour, minute))
    return sorted(set(slots))


def paced(config: dict[str, Any]) -> bool:
    return not digest_times(config)


def _minutes(text: Any, default: int) -> int:
    try:
        hour, minute = map(int, str(text).split(":"))
        return hour * 60 + minute
    except (TypeError, ValueError):
        return default


def waking_window(moment: datetime, config: dict[str, Any]) -> tuple[datetime, datetime]:
    """The waking window (local, from ``quiet_end`` to ``quiet_start``) that contains ``moment``, or the next one when
    ``moment`` falls in quiet hours. Without quiet hours the window is the calendar day."""
    notifications = config.get("notifications") or {}
    tz = _tz(config)
    local = moment.astimezone(tz)
    quiet_start = _minutes(notifications.get("quiet_start"), 22 * 60)
    quiet_end = _minutes(notifications.get("quiet_end"), 8 * 60)
    length = (quiet_start - quiet_end) % 1440 or 1440
    start_minute = quiet_end if quiet_start != quiet_end else 0
    # Same-zone arithmetic is wall-clock: on a DST change the window keeps its clock times, not its length.
    start = datetime.combine(local.date(), time(start_minute // 60, start_minute % 60), tzinfo=tz)
    if start > local:
        start = datetime.combine(local.date() - timedelta(days=1), start.time(), tzinfo=tz)
    end = start + timedelta(minutes=length)
    if local >= end:
        start = datetime.combine(start.date() + timedelta(days=1), start.time(), tzinfo=tz)
        end = start + timedelta(minutes=length)
    return start, end


def awake(moment: datetime, config: dict[str, Any]) -> bool:
    start, _ = waking_window(moment, config)
    return start <= moment.astimezone(start.tzinfo)


def latest_moment(deadline: datetime, config: dict[str, Any]) -> datetime:
    """The last moment a decision can reach the owner with time left to act: ``reminder_lead_minutes`` before its
    deadline, brought forward to the end of the previous waking window when that falls in quiet hours."""
    lead = int((config.get("notifications") or {}).get("reminder_lead_minutes", LEAD_MINUTES))
    moment = deadline - timedelta(minutes=lead)
    start, _ = waking_window(moment, config)
    if start <= moment.astimezone(start.tzinfo):
        return moment
    previous_start = datetime.combine(start.date() - timedelta(days=1), start.time(), tzinfo=start.tzinfo)
    _, previous_end = waking_window(previous_start, config)
    return previous_end - EDGE


def next_paced(now_dt: datetime, config: dict[str, Any], *, last_contact: datetime | None, reminders_in_window: int,
               waiting: int) -> datetime:
    """When the next regular reminder is due, given the last message the owner got (notice or reminder), the
    reminders already sent in this waking window and how many decisions wait. One reminder per decision waiting, as
    long as the day's ``reminders_per_day`` allows; what is left of the window is divided evenly among them."""
    notifications = config.get("notifications") or {}
    start, end = waking_window(now_dt, config)
    following = waking_window(end + timedelta(minutes=1), config)[0]
    if start > now_dt.astimezone(start.tzinfo):
        return start  # quiet hours now: the first of the morning
    per_day = max(1, int(notifications.get("reminders_per_day", REMINDERS_PER_DAY) or REMINDERS_PER_DAY))
    # What is left of the day only holds its share of the day's cap: twenty waiting at 21:50 are one more, not a burst.
    share = math.ceil(per_day * (end - now_dt) / (end - start))
    remaining = min(max(0, waiting), per_day - reminders_in_window, share)  # one per decision waiting, within the day's cap
    if remaining <= 0:
        return following
    if last_contact is None or last_contact < start:
        return start
    gap = reminder_gap(config)
    spacing = max(gap, (end - last_contact) / (remaining + 1))
    moment = last_contact + spacing
    return moment if moment < end else following


def reminder_gap(config: dict[str, Any]) -> timedelta:
    """The minimum between two messages (``reminder_gap_minutes``, none by default)."""
    value = (config.get("notifications") or {}).get("reminder_gap_minutes", MIN_GAP_MINUTES)
    return timedelta(minutes=max(0, int(value if value is not None else MIN_GAP_MINUTES)))


def next_delivery(now_dt: datetime, urgency: str, config: dict[str, Any]) -> datetime | None:
    """The moment a digest item of this urgency may go out; None means immediately (paced reminders choose when)."""
    slots = digest_times(config)
    if urgency == "now" or not slots:
        return None
    tz = _tz(config)
    local = now_dt.astimezone(tz)
    if urgency == "today":
        for slot in slots:
            candidate = local.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0)
            if candidate > local:
                return candidate
        first = slots[0]
        return (local + timedelta(days=1)).replace(hour=first.hour, minute=first.minute, second=0, microsecond=0)
    first = slots[0]
    morning = local.replace(hour=first.hour, minute=first.minute, second=0, microsecond=0)
    if morning > local:
        return morning
    return morning + timedelta(days=1)


def next_morning(now_dt: datetime, config: dict[str, Any]) -> datetime:
    """The start of the next waking window that is not the current one: «remind me tomorrow»."""
    start, end = waking_window(now_dt, config)
    if start > now_dt.astimezone(start.tzinfo):
        return start
    return waking_window(end + timedelta(minutes=1), config)[0]


def upcoming_slot(now_dt: datetime, config: dict[str, Any], lead_minutes: int) -> datetime | None:
    """The digest slot that starts within ``lead_minutes`` (now included), or None when none is that close (or the
    reminders are paced: see :func:`radar.delivery.dispatch.next_reminder_at`)."""
    slots = digest_times(config)
    if not slots or lead_minutes <= 0:
        return None
    tz = _tz(config)
    local = now_dt.astimezone(tz)
    horizon = local + timedelta(minutes=lead_minutes)
    for day_offset in (0, 1):
        day = local + timedelta(days=day_offset)
        for slot in slots:
            candidate = day.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0)
            if local <= candidate <= horizon:
                return candidate
    return None


def digest_due(now_dt: datetime, last_digest_at: datetime | None, config: dict[str, Any]) -> bool:
    """True when a digest slot has passed since the last digest (or every ``batch_minutes`` without slots)."""
    slots = digest_times(config)
    if not slots:
        minutes = int((config.get("notifications") or {}).get("batch_minutes", 120))
        return last_digest_at is None or last_digest_at + timedelta(minutes=minutes) <= now_dt
    tz = _tz(config)
    local = now_dt.astimezone(tz)
    latest: datetime | None = None
    for day_offset in (0, -1):
        day = local + timedelta(days=day_offset)
        for slot in slots:
            candidate = day.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0)
            if candidate <= local and (latest is None or candidate > latest):
                latest = candidate
    if latest is None:
        return False
    return last_digest_at is None or last_digest_at.astimezone(tz) < latest

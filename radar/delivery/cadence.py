"""When a non-urgent notice should reach the owner.

The planner judges urgency; code turns it into a delivery time. Anything that is not ``now`` waits for
the next digest slot (``notifications.digest_times``, local time): ``today`` takes the next slot of the
day, ``this_week`` and ``whenever`` wait for the first slot of a day, so quiet news arrives grouped in
the morning. Without configured slots the digest keeps its ``batch_minutes`` cadence.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

URGENCIES = ("now", "today", "this_week", "whenever")
DEFAULT_TIMES = ["09:00", "14:00", "19:00"]


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


def next_delivery(now_dt: datetime, urgency: str, config: dict[str, Any]) -> datetime | None:
    """The moment a digest item of this urgency may go out; None means immediately."""
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


def upcoming_slot(now_dt: datetime, config: dict[str, Any], lead_minutes: int) -> datetime | None:
    """The digest slot that starts within ``lead_minutes`` (now included), or None when none is that close."""
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

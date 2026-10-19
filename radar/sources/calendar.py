"""Calendar through a calendar provider: periodic window scan with a fingerprint per event.

Every sync lists the events between ``now - lookback_hours`` and ``now + lookahead_days`` and compares
each one with the fingerprint kept in the cursor: a new event is a ``created`` signal, a changed one
``updated``, one that disappears while still upcoming (or whose status says so) ``cancelled``. The first
sync only records the baseline unless ``initial_import`` is on, so a fresh install does not turn a whole
month of meetings into signals. Events the owner declined are not tracked.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..pipeline.triage import triage_signal
from ..providers.base import CalendarProvider, ProviderError
from .base import SourceAdapter, SyncResult, fingerprint, iso, parse_iso

log = logging.getLogger(__name__)

DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_LOOKAHEAD_DAYS = 30
MAX_TRACKED = 2000
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def event_fingerprint(event: dict[str, Any]) -> str:
    """What counts as a change: time, title, place, status, description, organiser and the owner's reply."""
    return fingerprint(event.get("start"), event.get("end"), event.get("title"), event.get("location"), event.get("status"),
                       (event.get("description") or "")[:2000], event.get("organizer"), event.get("my_response"))[:16]


def _when(event: dict[str, Any]) -> str:
    start, end = parse_iso(event.get("start")), parse_iso(event.get("end"))
    if start is None:
        return str(event.get("start") or "")
    if event.get("all_day"):
        return f"{start.day} {_MONTHS[start.month - 1]} {start.year} (all day)"
    text = f"{start.day} {_MONTHS[start.month - 1]} {start:%H:%M}"
    if end is not None:
        text += f"–{end:%H:%M}" if end.date() == start.date() else f" → {end.day} {_MONTHS[end.month - 1]} {end:%H:%M}"
    return text


class CalendarSource(SourceAdapter):
    kind = "calendar"

    def __init__(self, source_row: dict[str, Any], config: dict[str, Any], store: Any, *, provider: CalendarProvider,
                 preferences: list[dict[str, Any]] | None = None, **kwargs: Any):
        super().__init__(source_row, config, store, **kwargs)
        self.provider = provider
        self.preferences = preferences or []

    @property
    def calendar_id(self) -> str:
        return str(self.settings.get("calendar_id") or "primary")

    @property
    def interval_seconds(self) -> int:
        return max(60, int(self.settings.get("interval_seconds") or 600))

    def window(self, now_dt: datetime) -> tuple[datetime, datetime]:
        back = timedelta(hours=max(0, int(self.settings.get("lookback_hours") or DEFAULT_LOOKBACK_HOURS)))
        ahead = timedelta(days=max(1, int(self.settings.get("lookahead_days") or DEFAULT_LOOKAHEAD_DAYS)))
        return now_dt - back, now_dt + ahead

    def _signal(self, event: dict[str, Any], change: str, now_dt: datetime) -> dict[str, Any]:
        snippet_chars = int(self.config.get("retention", {}).get("snippet_chars", 500))
        account = str(self.settings.get("account") or self.row.get("account_ref") or self.calendar_id)
        attendees = [a.get("email") or a.get("name") for a in (event.get("attendees") or [])[:10]]
        head = f"{_when(event)}" + (f" · {event['location']}" if event.get("location") else "") + (f" · organizer {event['organizer']}" if event.get("organizer") else "")
        description = " ".join(str(event.get("description") or "").split())
        meta = {
            "source_kind": "calendar", "account": account, "calendar_id": event.get("calendar_id") or self.calendar_id, "event_id": event["id"],
            "change": change, "start": event.get("start"), "end": event.get("end"), "all_day": bool(event.get("all_day")), "status": event.get("status"),
            "location": event.get("location") or "", "organizer": event.get("organizer") or "", "attendees": attendees,
            "my_response": event.get("my_response") or "", "link": event.get("link") or "", "recurring_event_id": event.get("recurring_event_id") or "",
            "event_type": event.get("event_type") or "", "updated": event.get("updated") or "",
        }
        occurred = parse_iso(event.get("updated")) if change != "created" else None
        return {
            "id": str(uuid4()), "kind": "calendar", "occurred_at": iso(occurred or now_dt),
            "external_ref": f"{event['id']}@{change}:{event_fingerprint(event)}", "group_key": f"calendar:{self.id}:{event['id']}",
            "title": f"{event.get('title') or '(untitled)'}"[:300], "snippet": (f"[{change}] {head}. {description}").strip()[:snippet_chars],
            "meta": meta, "fingerprint": fingerprint("calendar", self.id, event["id"], change, event_fingerprint(event)),
        }

    def _ingest(self, signal: dict[str, Any], now_dt: datetime) -> str:
        decision = triage_signal(signal, self.preferences, self.config)
        added = self.store.add_signal(
            signal_id=signal["id"], source_id=self.id, kind="calendar", occurred_at=signal["occurred_at"], external_ref=signal["external_ref"],
            group_key=signal["group_key"], title=signal["title"], snippet=signal["snippet"], meta=signal["meta"], fingerprint=signal["fingerprint"],
            now=iso(now_dt), triage_status="pending" if decision.keep else "ignored", triage_reason=None if decision.keep else decision.reason,
            expires_at=self.signal_expiry(now_dt),
        )
        if not added:
            return "duplicate"
        return "added" if decision.keep else "ignored"

    def sync(self, now_dt: datetime) -> SyncResult:
        result = SyncResult(cursor=dict(self.cursor))
        self.push_requested = False
        time_min, time_max = self.window(now_dt)
        try:
            events = self.provider.list_events(time_min=iso(time_min), time_max=iso(time_max), calendar_id=self.calendar_id)
        except (ProviderError, PermissionError) as exc:
            result.error = str(exc)[:300]
            result.cursor["last_sync_at"] = iso(now_dt)
            result.health = {"status": "error", "error": result.error, "at": iso(now_dt)}
            return result
        known: dict[str, Any] = dict(result.cursor.get("events") or {})
        baseline = not known and not self.settings.get("initial_import")  # nothing tracked yet: record silently
        seen: dict[str, Any] = {}
        changes: list[tuple[dict[str, Any], str]] = []
        for event in events:
            result.seen += 1
            if event.get("my_response") == "declined":
                continue
            marker = event_fingerprint(event)
            seen[event["id"]] = {"fp": marker, "start": event.get("start") or "", "title": (event.get("title") or "")[:120]}
            previous = known.get(event["id"])
            if event.get("status") == "cancelled":
                if previous and previous.get("fp") != marker:
                    changes.append((event, "cancelled"))
                continue
            if previous is None:
                if not baseline:
                    changes.append((event, "created"))
            elif previous.get("fp") != marker:
                changes.append((event, "updated"))
        for event_id, previous in known.items():
            if event_id in seen or not isinstance(previous, dict):
                continue
            start = parse_iso(previous.get("start"))
            if start is not None and start > now_dt:  # vanished while still upcoming: cancelled or moved out of the window
                changes.append(({"id": event_id, "title": previous.get("title") or "(untitled)", "start": previous.get("start"), "status": "cancelled",
                                 "calendar_id": self.calendar_id, "attendees": []}, "cancelled"))
        for event, change in changes:
            try:
                outcome = self._ingest(self._signal(event, change, now_dt), now_dt)
            except Exception as exc:  # one bad event never stops the sweep
                log.warning("calendar ingest failed for %s: %s", event.get("id"), exc)
                continue
            if outcome == "added":
                result.added += 1
            elif outcome == "ignored":
                result.ignored += 1
        if len(seen) > MAX_TRACKED:
            seen = dict(sorted(seen.items(), key=lambda kv: kv[1].get("start") or "")[-MAX_TRACKED:])
        result.cursor["events"] = seen
        result.cursor["last_sync_at"] = iso(now_dt)
        result.health = {"status": "ok", "at": iso(now_dt), "seen": result.seen, "added": result.added, "baseline": baseline,
                         "window": [iso(time_min), iso(time_max)]}
        return result

from __future__ import annotations

import json
from datetime import timedelta

from conftest import NOW, config

from radar.pipeline.extract import signal_view
from radar.pipeline.triage import triage_signal
from radar.providers.calendar_gcal_mcp import GcalMcpProvider, parse_event_blocks, parse_events
from radar.providers.registry import build_calendar_provider
from radar.sources.calendar import CalendarSource, event_fingerprint

GOOGLE_EVENTS = {"events": [
    {"id": "ev1", "summary": "Vuelo MAD-LIS", "start": {"dateTime": "2026-11-03T07:10:00+01:00"}, "end": {"dateTime": "2026-11-03T08:30:00+01:00"},
     "status": "confirmed", "updated": "2026-10-19T09:00:00Z", "location": "Barajas T2", "htmlLink": "https://cal/ev1",
     "organizer": {"email": "me@example.org", "displayName": "David"}, "attendees": [{"email": "me@example.org", "self": True, "responseStatus": "accepted"}]},
    {"id": "ev2", "summary": "Reunión con Bernd", "start": {"dateTime": "2026-10-21T10:00:00+02:00"}, "end": {"dateTime": "2026-10-21T11:00:00+02:00"},
     "status": "confirmed", "updated": "2026-10-19T08:00:00Z", "organizer": {"email": "bernd@example.com"},
     "attendees": [{"email": "bernd@example.com", "responseStatus": "accepted"}, {"email": "me@example.org", "self": True, "responseStatus": "needsAction"}]},
    {"id": "ev3", "summary": "Cumpleaños", "start": {"date": "2026-10-25"}, "end": {"date": "2026-10-26"}, "status": "confirmed"},
    {"id": "ev4", "summary": "Declinada", "start": {"dateTime": "2026-10-22T10:00:00+02:00"}, "end": {"dateTime": "2026-10-22T11:00:00+02:00"},
     "attendees": [{"email": "me@example.org", "self": True, "responseStatus": "declined"}]},
]}
TEXT_LISTING = """Found 2 events between 2026-10-18 and 2026-11-18:

Vuelo MAD-LIS (ev1x8q2k)
Start: 2026-11-03T07:10:00+01:00
End: 2026-11-03T08:30:00+01:00
Location: Barajas T2
Status: confirmed
View: https://cal/ev1

Reunión con Bernd (ev2y9r3m)
Start: 2026-10-21T10:00:00+02:00
End: 2026-10-21T11:00:00+02:00
Organizer: bernd@example.com
Attendees: bernd@example.com (accepted), me@example.org (needsAction)
"""


def _mcp(payload):
    return {"ok": True, "result": payload}


def test_provider_normalises_structured_and_text_listings_and_refuses_writes():
    calls = []

    def call_mcp(server, tool, arguments, timeout=30):
        calls.append((server, tool, arguments))
        return _mcp({"content": [{"type": "text", "text": json.dumps(GOOGLE_EVENTS)}]})

    provider = GcalMcpProvider(call_mcp, "google-calendar", account="work", timezone="Europe/Madrid")
    events = provider.list_events(time_min="2026-10-18T22:00:17.969339Z", time_max="2026-11-18T00:00:00Z")
    assert calls[0][1] == "list-events" and calls[0][2]["calendarId"] == "primary" and calls[0][2]["account"] == "work" and calls[0][2]["timeZone"] == "Europe/Madrid"
    assert calls[0][2]["timeMin"] == "2026-10-19T00:00:17" and calls[0][2]["timeMax"] == "2026-11-18T01:00:00"  # local wall time, no fraction, no offset
    assert GcalMcpProvider(call_mcp, "google-calendar")._stamp("2026-10-18T22:00:00Z") == "2026-10-18T22:00:00"  # UTC when no zone is configured
    assert [e["id"] for e in events] == ["ev1", "ev2", "ev3", "ev4"]
    assert events[0]["start"] == "2026-11-03T07:10:00+01:00" and events[0]["organizer"] == "me@example.org" and events[0]["my_response"] == "accepted"
    assert events[1]["my_response"] == "needsaction" and events[1]["attendees"][0] == {"email": "bernd@example.com", "name": "", "response": "accepted"}
    assert events[2]["all_day"] is True and events[2]["start"] == "2026-10-25" and events[3]["my_response"] == "declined"
    # The same events as the server's text listing, or as structuredContent.
    text_events = parse_events(_mcp({"content": [{"type": "text", "text": TEXT_LISTING}]}), calendar_id="primary")
    assert [e["id"] for e in text_events] == ["ev1x8q2k", "ev2y9r3m"] and text_events[0]["title"] == "Vuelo MAD-LIS" and text_events[0]["location"] == "Barajas T2"
    assert text_events[1]["organizer"] == "bernd@example.com" and text_events[1]["attendees"][1]["response"] == "needsaction" and text_events[1]["calendar_id"] == "primary"
    assert [e["id"] for e in parse_events(_mcp({"structuredContent": {"items": GOOGLE_EVENTS["events"][:1]}}))] == ["ev1"]
    assert parse_events(_mcp({"content": [{"type": "text", "text": "No events found."}]})) == []
    assert parse_event_blocks("Lunch (abcde)\nStart: 2026-10-20T13:00:00+02:00")[0]["id"] == "abcde"
    for forbidden in ("respond", ):
        try:
            getattr(provider, forbidden)("ev2", "accepted")
            raise AssertionError("write must be refused")
        except PermissionError:
            pass
    assert build_calendar_provider(call_mcp, {"id": "x", "server": "google-calendar"}).provider_id == "gcal_mcp"
    try:
        build_calendar_provider(call_mcp, {"id": "x", "server": "google-calendar", "provider": "other"})
        raise AssertionError("unknown provider accepted")
    except ValueError:
        pass


class FakeCalendar:
    def __init__(self, events):
        self.events = events
        self.windows = []

    def list_events(self, *, time_min, time_max, calendar_id="primary"):
        self.windows.append((time_min, time_max, calendar_id))
        return [dict(e) for e in self.events]


def _source(store, provider, **settings):
    cfg = config()
    store.sync_source(source_id="cal", kind="calendar", account_ref="primary", config={"server": "google-calendar", "calendar_id": "primary", **settings},
                      resource_scope={"calendars": ["primary"]}, retention_days=30, enabled=True, operations=["calendar.read"], now="2026-10-19T08:00:00Z")
    row = store.get_source("configured:cal")
    return CalendarSource(row, cfg, store, provider=provider, clock=lambda: NOW)


def _events():
    return [
        {"id": "ev1", "title": "Vuelo MAD-LIS", "start": "2026-11-03T07:10:00+01:00", "end": "2026-11-03T08:30:00+01:00", "status": "confirmed", "updated": "2026-10-19T09:00:00Z",
         "location": "Barajas T2", "organizer": "me@example.org", "attendees": [{"email": "me@example.org", "name": "", "response": "accepted"}], "my_response": "accepted", "all_day": False, "description": ""},
        {"id": "ev2", "title": "Reunión con Bernd", "start": "2026-10-21T10:00:00+02:00", "end": "2026-10-21T11:00:00+02:00", "status": "confirmed", "updated": "2026-10-19T08:00:00Z",
         "organizer": "bernd@example.com", "attendees": [{"email": "bernd@example.com", "name": "", "response": "accepted"}], "my_response": "needsAction", "all_day": False, "description": "Repasar el bug de subida"},
        {"id": "ev4", "title": "Declinada", "start": "2026-10-22T10:00:00+02:00", "end": "2026-10-22T11:00:00+02:00", "status": "confirmed", "my_response": "declined", "attendees": [], "all_day": False},
    ]


def test_calendar_source_baselines_first_then_signals_created_updated_and_cancelled(store):
    provider = FakeCalendar(_events())
    source = _source(store, provider, lookback_hours=12, lookahead_days=7)
    first = source.sync(NOW)
    assert first.error is None and first.seen == 3 and first.added == 0 and first.health["baseline"] is True
    assert provider.windows[0] == ("2026-10-18T22:00:00Z", "2026-10-26T10:00:00Z", "primary")
    assert set(first.cursor["events"]) == {"ev1", "ev2"}  # the declined one is not tracked
    store.update_source_cursor(source.id, first.cursor, "2026-10-19T10:00:00Z", success=True)
    source.row = store.get_source(source.id)
    # Second sync: one new event, one moved, one vanished while upcoming.
    later = NOW + timedelta(minutes=10)
    events = _events()
    events[1] = {**events[1], "start": "2026-10-21T12:00:00+02:00", "end": "2026-10-21T13:00:00+02:00", "updated": "2026-10-19T10:05:00Z"}
    events.append({"id": "ev5", "title": "Entrega del informe", "start": "2026-10-23T09:00:00+02:00", "end": "2026-10-23T09:30:00+02:00", "status": "confirmed",
                   "organizer": "boss@example.org", "attendees": [], "my_response": "", "all_day": False, "description": "Deadline interno"})
    provider.events = [e for e in events if e["id"] != "ev1"]
    second = source.sync(later)
    assert second.error is None and second.added == 3 and second.health["baseline"] is False
    signals = {s["meta"]["event_id"]: s for s in store.list_signals(kind="calendar", limit=20)}
    assert signals["ev5"]["meta"]["change"] == "created" and signals["ev5"]["snippet"].startswith("[created] 23 Oct 09:00–09:30 · organizer boss@example.org. Deadline interno")
    assert signals["ev2"]["meta"]["change"] == "updated" and signals["ev2"]["meta"]["start"] == "2026-10-21T12:00:00+02:00" and signals["ev2"]["occurred_at"] == "2026-10-19T10:05:00Z"
    assert signals["ev1"]["meta"]["change"] == "cancelled" and signals["ev1"]["title"] == "Vuelo MAD-LIS" and signals["ev1"]["group_key"] == "calendar:configured:cal:ev1"
    assert all(s["triage_status"] == "pending" for s in signals.values())
    # A third sync with nothing changed adds nothing; an RSVP by another guest is not a change either.
    store.update_source_cursor(source.id, second.cursor, "2026-10-19T10:10:00Z", success=True)
    source.row = store.get_source(source.id)
    provider.events[0]["attendees"] = [{"email": "bernd@example.com", "name": "", "response": "tentative"}]
    third = source.sync(later + timedelta(minutes=10))
    assert third.added == 0 and third.seen == 3 and set(third.cursor["events"]) == {"ev2", "ev5"}
    # A provider failure is reported in health and keeps the cursor.
    class Broken:
        def list_events(self, **kwargs):
            from radar.providers.base import ProviderError
            raise ProviderError("boom")

    broken = _source(store, Broken())
    assert broken.sync(NOW).health["status"] == "error"
    # Fingerprint ignores volatile fields but sees moves, renames and the owner's reply.
    base = _events()[1]
    assert event_fingerprint(base) == event_fingerprint({**base, "updated": "x", "attendees": []})
    assert event_fingerprint(base) != event_fingerprint({**base, "my_response": "accepted"}) != event_fingerprint({**base, "title": "Otra"})


def test_calendar_signals_are_kept_by_triage_and_shown_to_the_model(store):
    source = _source(store, FakeCalendar([]))
    signal = source._signal(_events()[1], "created", NOW)
    assert triage_signal(signal, [], config()).keep is True
    view = signal_view(signal, budget=4000)
    assert view["change"] == "created" and view["start"] == "2026-10-21T10:00:00+02:00" and view["organizer"] == "bernd@example.com"
    assert view["my_response"] == "needsAction" and view["attendees"] == ["bernd@example.com"] and "Repasar el bug" in view["body"]
    assert signal["meta"]["account"] == "primary" and signal["external_ref"].startswith("ev2@created:")

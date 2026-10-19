"""Adapter for the Google Calendar MCP family (@cocal/google-calendar-mcp and forks).

Read-only by default: ``list-events`` / ``get-event`` over the host's ``call_mcp``. Answers come either as
structured JSON (Google API event objects) or as the server's text listing; both are normalised to the
same event dict so the source never depends on the exact server version.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import mcp_parsing as parse
from .base import CalendarProvider, ProviderError

READ_TOOLS = frozenset({"list-events", "search-events", "get-event", "list-calendars", "get-freebusy", "get-current-time", "list-colors"})
WRITE_TOOLS = frozenset({"create-event", "create-events", "update-event", "delete-event", "respond-to-event"})
EXTRA_FIELDS = ["description", "location", "attendees", "organizer", "status", "updated", "htmlLink", "recurringEventId", "eventType", "created"]
MAX_ATTENDEES = 12

_BLOCK_KEYS = {
    "start": "start", "starts": "start", "end": "end", "ends": "end", "location": "location", "where": "location",
    "description": "description", "notes": "description", "status": "status", "updated": "updated", "last updated": "updated",
    "organizer": "organizer", "attendees": "attendees", "guests": "attendees", "view": "link", "link": "link", "url": "link",
    "html link": "link", "event id": "id", "id": "id", "calendar": "calendar_id", "calendar id": "calendar_id",
    "your response": "my_response", "response": "my_response", "recurring event id": "recurring_event_id", "type": "event_type",
}
_TITLE_WITH_ID = re.compile(r"^(?P<title>.*?)\s*\((?P<id>[A-Za-z0-9_@.\-]{5,})\)\s*$")
_FOUND_LINE = re.compile(r"^(found|no)\s+\d*\s*events?\b|^\d+ events?\b|^events? (in|between|from)\b", re.I)


def _time_of(value: Any) -> tuple[str, bool]:
    """Google ``{"dateTime": ...}`` / ``{"date": ...}`` or a plain string → (ISO text, all_day)."""
    if isinstance(value, dict):
        if value.get("dateTime"):
            return str(value["dateTime"]), False
        if value.get("date"):
            return str(value["date"]), True
        return "", False
    text = str(value or "").strip()
    return text, bool(text) and len(text) == 10 and text[4] == "-"


def _attendees_of(value: Any) -> tuple[list[dict[str, str]], str]:
    """Normalised attendee list (email, name, response) and the owner's own response when marked."""
    attendees: list[dict[str, str]] = []
    mine = ""
    if isinstance(value, str):
        value = [v.strip() for v in re.split(r"[;,]\s*", value) if v.strip()]
    for item in value or []:
        if isinstance(item, dict):
            email = str(item.get("email") or "")
            entry = {"email": email, "name": str(item.get("displayName") or item.get("name") or ""), "response": str(item.get("responseStatus") or item.get("response") or "")}
            if item.get("self"):
                mine = entry["response"]
        else:
            text = str(item)
            match = re.match(r"^(?P<name>.*?)\s*<?(?P<email>[^\s<>()]+@[^\s<>()]+)>?\s*(?:\((?P<resp>[a-zA-Z]+)\))?$", text)
            entry = {"email": match.group("email"), "name": match.group("name").strip(' "'), "response": (match.group("resp") or "").lower()} if match else {"email": "", "name": text, "response": ""}
        if entry["email"] or entry["name"]:
            attendees.append(entry)
        if len(attendees) >= MAX_ATTENDEES:
            break
    return attendees, mine


def normalise_event(raw: dict[str, Any], *, calendar_id: str = "") -> dict[str, Any] | None:
    """One event dict in the shape the source understands, or None when it has no id."""
    event_id = str(raw.get("id") or raw.get("eventId") or raw.get("event_id") or "").strip()
    if not event_id:
        return None
    start, all_day = _time_of(raw.get("start"))
    end, _ = _time_of(raw.get("end"))
    organizer = raw.get("organizer")
    if isinstance(organizer, dict):
        organizer = str(organizer.get("email") or organizer.get("displayName") or "")
    attendees, mine = _attendees_of(raw.get("attendees"))
    my_response = str(raw.get("my_response") or raw.get("responseStatus") or mine or "").lower()
    return {
        "id": event_id, "title": str(raw.get("summary") or raw.get("title") or "(untitled)").strip(), "start": start, "end": end,
        "all_day": bool(all_day), "status": str(raw.get("status") or "confirmed").lower(), "updated": str(raw.get("updated") or ""),
        "location": str(raw.get("location") or ""), "description": str(raw.get("description") or ""),
        "organizer": str(organizer or ""), "attendees": attendees, "my_response": my_response,
        "link": str(raw.get("htmlLink") or raw.get("link") or raw.get("url") or ""), "calendar_id": str(raw.get("calendar_id") or raw.get("calendarId") or calendar_id or ""),
        "recurring_event_id": str(raw.get("recurringEventId") or raw.get("recurring_event_id") or ""), "event_type": str(raw.get("eventType") or raw.get("event_type") or ""),
    }


def _events_from_json(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, dict):
        for key in ("events", "items", "results", "data"):
            if isinstance(value.get(key), list):
                return [v for v in value[key] if isinstance(v, dict)]
        return [value] if value.get("id") and (value.get("start") or value.get("summary")) else None
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return None


def parse_event_blocks(text: str) -> list[dict[str, Any]]:
    """The server's text listing: blocks separated by blank lines, ``Key: value`` lines, title first."""
    events: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if len(lines) == 1 and _FOUND_LINE.match(lines[0].strip()):
            continue
        raw: dict[str, Any] = {}
        for index, line in enumerate(lines):
            stripped = line.strip().lstrip("-•* ").strip()
            key, sep, value = stripped.partition(":")
            if sep and key.strip().lower() in _BLOCK_KEYS and not (index == 0 and key.strip().lower() not in {"event id", "id"} and "title" not in raw and len(key) > 20):
                raw[_BLOCK_KEYS[key.strip().lower()]] = value.strip()
                continue
            if index == 0 or ("title" not in raw and "id" not in raw):
                match = _TITLE_WITH_ID.match(stripped)
                if match:
                    raw["title"], raw["id"] = match.group("title") or "(untitled)", match.group("id")
                elif not _FOUND_LINE.match(stripped):
                    raw.setdefault("title", stripped)
        if raw.get("id"):
            events.append(raw)
    return events


def parse_events(payload: Any, *, calendar_id: str = "") -> list[dict[str, Any]]:
    """Events from an MCP answer: structured content, JSON text or the text listing."""
    inner = parse.unwrap(payload)
    candidates: list[dict[str, Any]] | None = None
    if isinstance(inner, dict) and isinstance(inner.get("structuredContent"), (dict, list)):
        candidates = _events_from_json(inner["structuredContent"])
    if candidates is None:
        candidates = _events_from_json(inner) if isinstance(inner, (dict, list)) and not (isinstance(inner, dict) and "content" in inner) else None
    if candidates is None:
        text = parse.content_texts(payload)
        stripped = text.strip()
        if stripped[:1] in "[{":
            try:
                candidates = _events_from_json(json.loads(stripped))
            except ValueError:
                candidates = None
        if candidates is None:
            candidates = parse_event_blocks(text)
    events = []
    for raw in candidates or []:
        event = normalise_event(raw, calendar_id=calendar_id)
        if event is not None:
            events.append(event)
    return events


class GcalMcpProvider(CalendarProvider):
    provider_id = "gcal_mcp"

    def __init__(self, call_mcp: Callable[..., Any], server: str, *, timeout: float = 30, allow_write: bool = False,
                 account: str = "", timezone: str = ""):
        if not server or not callable(call_mcp):
            raise ValueError("server and call_mcp are required")
        self.call_mcp = call_mcp
        self.server = server
        self.timeout = timeout
        self.read_only = not allow_write
        self.account = account
        self.timezone = timezone

    def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if tool not in READ_TOOLS and (self.read_only or tool not in WRITE_TOOLS):
            raise PermissionError(f"{tool} is not permitted on this provider")
        args = {k: v for k, v in arguments.items() if v not in (None, "", [])}
        if self.account:
            args.setdefault("account", self.account)
        try:
            return self.call_mcp(self.server, tool, args, timeout=self.timeout)
        except PermissionError:
            raise
        except Exception as exc:
            raise ProviderError(f"{self.server}/{tool}: {type(exc).__name__}: {str(exc)[:200]}") from exc

    def _zone(self) -> Any:
        try:
            return ZoneInfo(self.timezone) if self.timezone else timezone.utc
        except Exception:
            return timezone.utc

    def _stamp(self, value: str) -> str:
        """The server validates ``YYYY-MM-DDTHH:MM:SS`` (no fraction, no offset) interpreted in ``timeZone``."""
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return str(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(self._zone()).strftime("%Y-%m-%dT%H:%M:%S")

    def list_events(self, *, time_min: str, time_max: str, calendar_id: str = "primary") -> list[dict[str, Any]]:
        zone = self._zone()
        raw = self._call("list-events", {"calendarId": calendar_id, "timeMin": self._stamp(time_min), "timeMax": self._stamp(time_max),
                                         "timeZone": getattr(zone, "key", None) or "UTC", "fields": EXTRA_FIELDS})
        try:
            return parse_events(raw, calendar_id=calendar_id)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc

    def get_event(self, event_id: str, *, calendar_id: str = "primary") -> dict[str, Any]:
        raw = self._call("get-event", {"calendarId": calendar_id, "eventId": event_id, "fields": EXTRA_FIELDS})
        try:
            events = parse_events(raw, calendar_id=calendar_id)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc
        if not events:
            raise ProviderError(f"event {event_id} not found")
        return events[0]

    def respond(self, event_id: str, response: str, *, calendar_id: str = "primary", comment: str = "") -> dict[str, Any]:
        """Accept/decline/tentative. External (notifies the organiser): only with explicit consent upstream."""
        raw = self._call("respond-to-event", {"calendarId": calendar_id, "eventId": event_id, "response": response, "comment": comment})
        return {"ok": True, "text": parse.content_texts(raw)[:500]}

    def capabilities(self) -> dict[str, bool]:
        return {"read": True, "respond": not self.read_only}

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fingerprint(*parts: Any) -> str:
    joined = "\x1f".join(" ".join(str(p or "").split()).lower() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass
class SyncResult:
    added: int = 0
    seen: int = 0
    ignored: int = 0
    cursor: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    health: dict[str, Any] = field(default_factory=dict)


class SourceAdapter:
    kind = ""

    def __init__(self, source_row: dict[str, Any], config: dict[str, Any], store: Any, *, clock: Callable[[], datetime] = utc_now):
        self.row = source_row
        self.config = config
        self.store = store
        self.clock = clock
        self.push_requested = False

    @property
    def id(self) -> str:
        return str(self.row["id"])

    @property
    def settings(self) -> dict[str, Any]:
        return self.row.get("config") or {}

    @property
    def cursor(self) -> dict[str, Any]:
        return self.row.get("cursor") or {}

    @property
    def interval_seconds(self) -> int:
        return max(60, int(self.settings.get("interval_seconds") or 300))

    def due(self, now_dt: datetime) -> bool:
        if self.push_requested:
            return True
        last = parse_iso(self.cursor.get("last_sync_at"))
        return last is None or last + timedelta(seconds=self.interval_seconds) <= now_dt

    def handle_push(self, payload: dict[str, Any]) -> None:
        self.push_requested = True

    def sync(self, now_dt: datetime) -> SyncResult:
        raise NotImplementedError

    def signal_expiry(self, now_dt: datetime) -> str:
        days = int(self.config.get("retention", {}).get("signals_days", 90))
        return iso(now_dt + timedelta(days=days))

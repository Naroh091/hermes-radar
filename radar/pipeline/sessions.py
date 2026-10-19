"""Active chat sessions: the owner and the agent are already on it there.

A ``session_turn`` bundle means the conversation itself is doing the work; Radar only learns from it once
the session has been quiet for ``observation.session_quiet_minutes``. Tasks born from a session keep its id
in ``entities.session_id``; while that session is active they stay silent and never spawn parallel jobs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_QUIET_MINUTES = 30


def quiet_minutes(config: dict[str, Any]) -> int:
    try:
        return max(0, int((config.get("observation") or {}).get("session_quiet_minutes", DEFAULT_QUIET_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_QUIET_MINUTES


def session_of(signals: list[dict[str, Any]]) -> str:
    for signal in signals:
        if signal.get("kind") == "session_turn":
            sid = str((signal.get("meta") or {}).get("session_id") or "")
            if sid:
                return sid
    return ""


def last_turn_at(store: Any, session_id: str) -> datetime | None:
    if not session_id:
        return None
    try:
        value = store.last_session_turn_at(session_id)
    except Exception:
        return None
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def session_active(store: Any, session_id: str, now_dt: datetime, config: dict[str, Any]) -> bool:
    """True while the session's last recorded turn is younger than the quiet window."""
    minutes = quiet_minutes(config)
    if not session_id or minutes <= 0:
        return False
    last = last_turn_at(store, session_id)
    return last is not None and now_dt - last < timedelta(minutes=minutes)


def task_session_active(store: Any, task: dict[str, Any] | None, now_dt: datetime, config: dict[str, Any]) -> bool:
    if not task:
        return False
    sid = str((task.get("entities") or {}).get("session_id") or "")
    return session_active(store, sid, now_dt, config)

"""Conversation turns as signals: hooks in-process, plus a state.db poller as a safety net."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from .base import SourceAdapter, SyncResult, fingerprint, iso, parse_iso

log = logging.getLogger(__name__)

SOURCE_ID = "local:session-hooks"
MAX_USER = 4000
MAX_ASSISTANT = 2000
EXCLUDED_PLATFORMS = {"cron", "webhook", "subagent", "api_server", "kanban", "msgraph_webhook"}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return "" if value is None else str(value)


def record_turn(store: Any, config: dict[str, Any], event: dict[str, Any], now_dt: datetime) -> bool:
    """Persist one finished turn as a debounced ``session_turn`` signal."""
    platform = str(event.get("platform") or "")
    if platform in EXCLUDED_PLATFORMS:
        return False
    user_text = _text(event.get("user_message")).strip()
    assistant_text = _text(event.get("assistant_response")).strip()
    if not user_text:
        return False
    session_id = str(event.get("session_id") or "")
    turn_id = str(event.get("turn_id") or "")
    now = iso(now_dt)
    retention = config.get("retention", {})
    observation = config.get("observation", {})
    store.ensure_local_observation_source(now, int(retention.get("observations_days", 30)))
    debounce = int(observation.get("debounce_seconds", 120))
    external_ref = turn_id or fingerprint(session_id, user_text[:200], now[:16])
    snippet = " ".join(user_text.split())[:int(retention.get("snippet_chars", 500))]
    meta = {
        "session_id": session_id, "platform": platform, "turn_id": turn_id, "model": str(event.get("model") or ""),
        "user_text": user_text[:MAX_USER], "assistant_text": assistant_text[:MAX_ASSISTANT],
        "source_kind": "session", "radar_origin": bool(event.get("radar_origin")),
    }
    return store.add_signal(
        signal_id=str(uuid4()), source_id=SOURCE_ID, kind="session_turn", occurred_at=now, external_ref=external_ref,
        group_key=f"session:{session_id or external_ref}", title=snippet[:120], snippet=snippet, meta=meta,
        fingerprint=fingerprint("turn", session_id, user_text[:500]), now=now,
        next_attempt_at=iso(now_dt + timedelta(seconds=debounce)),
        expires_at=iso(now_dt + timedelta(days=int(retention.get("signals_days", 90)))),
    )


def record_subagent(store: Any, config: dict[str, Any], event: dict[str, Any], now_dt: datetime) -> bool:
    summary = _text(event.get("child_summary")).strip()
    if not summary:
        return False
    now = iso(now_dt)
    retention = config.get("retention", {})
    store.ensure_local_observation_source(now, int(retention.get("observations_days", 30)))
    parent = str(event.get("parent_session_id") or "")
    child = str(event.get("child_session_id") or "")
    meta = {
        "session_id": parent, "child_session_id": child, "role": str(event.get("child_role") or ""),
        "status": str(event.get("child_status") or ""), "user_text": _text(event.get("child_goal"))[:1000],
        "assistant_text": summary[:MAX_ASSISTANT], "source_kind": "session", "platform": "subagent",
    }
    return store.add_signal(
        signal_id=str(uuid4()), source_id=SOURCE_ID, kind="session_turn", occurred_at=now,
        external_ref=f"subagent:{child or uuid4().hex}", group_key=f"session:{parent or child}",
        title=summary[:120], snippet=" ".join(summary.split())[:500], meta=meta,
        fingerprint=fingerprint("subagent", child, summary[:500]), now=now,
        next_attempt_at=iso(now_dt + timedelta(seconds=int(config.get("observation", {}).get("debounce_seconds", 120)))),
        expires_at=iso(now_dt + timedelta(days=int(retention.get("signals_days", 90)))),
    )


class SessionDbPoller(SourceAdapter):
    """Safety net for turns produced by processes where the hooks did not fire (other profiles, crashes)."""

    kind = "session"

    def __init__(self, source_row: dict[str, Any], config: dict[str, Any], store: Any, *, db_factory: Any = None, **kwargs: Any):
        super().__init__(source_row, config, store, **kwargs)
        self.db_factory = db_factory

    @property
    def interval_seconds(self) -> int:
        return max(60, int(self.config.get("observation", {}).get("state_db_poll_seconds", 300)))

    def _open(self) -> Any:
        if self.db_factory is not None:
            return self.db_factory()
        from hermes_state import SessionDB  # host internal reader, used read-only
        return SessionDB(read_only=True)

    def sync(self, now_dt: datetime) -> SyncResult:
        result = SyncResult(cursor=dict(self.cursor))
        cursors: dict[str, int] = dict(result.cursor.get("sessions") or {})
        try:
            db = self._open()
        except Exception as exc:
            result.error = f"state.db unavailable: {str(exc)[:200]}"
            result.cursor["last_sync_at"] = iso(now_dt)
            return result
        try:
            sessions = db.list_sessions_rich(limit=20, order_by_last_active=True, exclude_sources=sorted(EXCLUDED_PLATFORMS))
            for session in sessions:
                session_id = str(session.get("id") or session.get("session_id") or "")
                if not session_id:
                    continue
                after_id = cursors.get(session_id)
                messages = db.get_messages(session_id, after_id=after_id, limit=200) if after_id else db.get_messages(session_id, latest=True, limit=40)
                last_user: dict[str, Any] | None = None
                max_id = after_id or 0
                for message in messages:
                    message_id = int(message.get("id") or 0)
                    max_id = max(max_id, message_id)
                    role = message.get("role")
                    if role == "user":
                        last_user = message
                    elif role == "assistant" and last_user is not None:
                        event = {
                            "session_id": session_id, "platform": str(session.get("source") or ""),
                            "turn_id": f"state:{session_id}:{message_id}",
                            "user_message": last_user.get("content"), "assistant_response": message.get("content"),
                        }
                        result.seen += 1
                        if record_turn(self.store, self.config, event, now_dt):
                            result.added += 1
                        last_user = None
                if max_id:
                    cursors[session_id] = max_id
        except Exception as exc:
            result.error = f"state.db poll failed: {str(exc)[:200]}"
        finally:
            try:
                db.close()
            except Exception:
                pass
        result.cursor["sessions"] = dict(list(cursors.items())[-200:])
        result.cursor["last_sync_at"] = iso(now_dt)
        result.health = {"status": "error" if result.error else "ok", "sessions": len(cursors)}
        return result

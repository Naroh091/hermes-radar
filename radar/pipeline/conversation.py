"""A chat session judged as a whole.

One turn says little: a request opened in one message is usually done two messages later, and a
question is answered in the next reply. Radar therefore never evaluates a ``session_turn`` on its own.
When a session has been quiet long enough, the pending turns are bundled with the rest of the
session's transcript (turns already evaluated stay as context) and the model judges where the
conversation ended: resolved, still open, or deferred on purpose.
"""
from __future__ import annotations

from typing import Any

from ..redact import redact
from .sessions import session_of

MAX_TURNS = 40
MAX_CHARS = 16000
MAX_OWNER = 1500
MAX_AGENT = 1200
CONVERSATION_STATES = ("resolved", "open", "deferred", "not_a_conversation")


def is_conversation(signals: list[dict[str, Any]]) -> bool:
    return bool(signals) and all(s.get("kind") == "session_turn" for s in signals) and bool(session_of(signals))


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _turn(row: dict[str, Any], new_ids: set[str]) -> dict[str, Any]:
    meta = row.get("meta") or {}
    turn = {
        "at": row.get("occurred_at"),
        "owner_said": redact(_clip(meta.get("user_text") or row.get("snippet"), MAX_OWNER)),
        "agent_said": redact(_clip(meta.get("assistant_text"), MAX_AGENT)),
        "new": row.get("id") in new_ids,
    }
    if meta.get("platform") == "subagent":
        turn["subagent"] = True
    return turn


def transcript(store: Any, session_id: str, new_ids: set[str], *, max_turns: int = MAX_TURNS, max_chars: int = MAX_CHARS) -> list[dict[str, Any]]:
    """The session's turns oldest first, trimmed from the start to fit the budget; new turns always stay."""
    rows = store.list_signals(kind="session_turn", group_key=f"session:{session_id}", limit=max(50, max_turns * 3))
    rows = sorted(rows, key=lambda r: (str(r.get("occurred_at") or ""), str(r.get("id") or "")))
    kept: list[dict[str, Any]] = []
    used = 0
    for row in reversed(rows):
        turn = _turn(row, new_ids)
        size = len(turn["owner_said"]) + len(turn["agent_said"]) + 40
        if not turn["new"] and (used + size > max_chars or len(kept) >= max_turns):
            continue
        kept.append(turn)
        used += size
    kept.reverse()
    return kept


def view(session_id: str, signals: list[dict[str, Any]], turns: list[dict[str, Any]]) -> dict[str, Any]:
    meta = (signals[0].get("meta") or {}) if signals else {}
    return {
        "session_id": session_id,
        "platform": str(meta.get("platform") or ""),
        "turns": turns,
        "new_turns": sum(1 for t in turns if t.get("new")),
        "signal_ids": [s.get("id") for s in signals],
    }

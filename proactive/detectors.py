from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .models import canonical_hash


@dataclass(frozen=True)
class Candidate:
    detector: str
    reason: str
    subject_key: str
    status: str
    next_check_at: datetime | None
    confidence_band: str
    assumptions: dict[str, Any]


_DURATION = re.compile(r"\ben\s+(\d{1,3})\s*(minuto|minutos|hora|horas|día|días)\b", re.IGNORECASE)
_COMMITMENT = re.compile(
    r"\b(te avisar[ée]|har[ée] seguimiento|lo comprobar[ée]|volver[ée] a comprobar)\b",
    re.IGNORECASE,
)
_DEADLINE = re.compile(r"\b(mañana|hoy|esta tarde|esta noche)\b", re.IGNORECASE)


def _due_from_text(text: str, now: datetime) -> datetime | None:
    match = _DURATION.search(text)
    if match:
        value = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("minuto"):
            return now + timedelta(minutes=value)
        if unit.startswith("hora"):
            return now + timedelta(hours=value)
        return now + timedelta(days=value)
    marker = _DEADLINE.search(text)
    if not marker:
        return None
    value = marker.group(1).lower()
    if value == "hoy":
        return now + timedelta(hours=4)
    if value == "esta tarde":
        return now.replace(hour=18, minute=0, second=0, microsecond=0)
    if value == "esta noche":
        return now.replace(hour=21, minute=0, second=0, microsecond=0)
    return now + timedelta(days=1)


def detect(kind: str, event: dict[str, Any], now: datetime) -> list[Candidate]:
    """Conservative local detectors. They persist no raw conversation content."""
    candidates: list[Candidate] = []
    if kind == "llm_call":
        response = event.get("assistant_response")
        user_message = event.get("user_message")
        if isinstance(response, str) and _COMMITMENT.search(response):
            due = _due_from_text(response, now)
            if due is not None:
                normalized = _COMMITMENT.search(response).group(1).lower()
                candidates.append(
                    Candidate(
                        detector="explicit_commitment", reason="agent_commitment",
                        subject_key="commitment:" + canonical_hash({"phrase": normalized, "turn": event.get("turn_id")})[:24],
                        status="candidate", next_check_at=due, confidence_band="high",
                        assumptions={"detector": "explicit_commitment", "turn_id": event.get("turn_id")},
                    )
                )
        if isinstance(user_message, str):
            due = _due_from_text(user_message, now)
            if due is not None and any(word in user_message.lower() for word in ("antes de", "plazo", "vence", "recuérdame")):
                candidates.append(
                    Candidate(
                        detector="deadline", reason="next_step",
                        subject_key="deadline:" + canonical_hash({"turn": event.get("turn_id"), "due": due.isoformat()})[:24],
                        status="candidate", next_check_at=due, confidence_band="medium",
                        assumptions={"detector": "deadline", "turn_id": event.get("turn_id")},
                    )
                )
    if kind == "tool_call" and event.get("status") in {"failed", "error"}:
        error_type = str(event.get("error_type") or "")
        if error_type in {"MissingInputError", "ApprovalRequired", "AuthenticationRequired"}:
            candidates.append(
                Candidate(
                    detector="missing_input", reason="missing_input",
                    subject_key="missing-input:" + canonical_hash(
                        {"tool": event.get("tool_name"), "turn": event.get("turn_id"), "error": error_type}
                    )[:24],
                    status="waiting_input", next_check_at=None, confidence_band="high",
                    assumptions={
                        "detector": "missing_input", "tool_name": event.get("tool_name"),
                        "error_type": error_type, "turn_id": event.get("turn_id"),
                    },
                )
            )
    return candidates

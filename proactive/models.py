from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final


class StateTransitionError(ValueError):
    """Raised when a persisted state machine transition is not allowed."""


FOLLOWUP_TERMINAL: Final = frozenset({"completed", "cancelled", "expired"})

TRANSITIONS: Final[dict[str, dict[str, frozenset[str]]]] = {
    "followup": {
        "candidate": frozenset({"ready", "waiting_input", "waiting_event", "suppressed", "paused", "cancelled", "expired"}),
        "ready": frozenset({"checking", "waiting_event", "waiting_input", "awaiting_consent", "completed", "failed", "paused", "cancelled", "expired"}),
        "checking": frozenset({"ready", "waiting_event", "waiting_input", "awaiting_consent", "completed", "failed", "paused", "cancelled", "expired"}),
        "waiting_input": frozenset({"ready", "waiting_event", "suppressed", "paused", "cancelled", "expired"}),
        "waiting_event": frozenset({"ready", "waiting_input", "suppressed", "paused", "cancelled", "expired"}),
        "awaiting_consent": frozenset({"ready", "suppressed", "waiting_event", "paused", "cancelled", "expired"}),
        "suppressed": frozenset({"ready", "paused", "cancelled", "expired"}),
        "failed": frozenset({"ready", "paused", "cancelled", "expired"}),
        "paused": frozenset({"ready", "cancelled", "expired"}),
        "completed": frozenset(),
        "cancelled": frozenset(),
        "expired": frozenset(),
    },
    "proposal": {
        "draft": frozenset({"pending", "cancelled", "expired"}),
        "pending": frozenset({"accepted", "rejected", "expired", "cancelled"}),
        "accepted": frozenset({"executing", "cancelled", "expired"}),
        "executing": frozenset({"succeeded", "failed", "unknown"}),
        "rejected": frozenset(),
        "expired": frozenset(),
        "cancelled": frozenset(),
        "succeeded": frozenset(),
        "failed": frozenset(),
        "unknown": frozenset(),
    },
    "run": {
        "queued": frozenset({"running", "cancelled", "failed"}),
        "running": frozenset({"succeeded", "failed", "unknown", "cancelled"}),
        "succeeded": frozenset(),
        "failed": frozenset(),
        "unknown": frozenset(),
        "cancelled": frozenset(),
    },
    "outbox": {
        "pending": frozenset({"sending", "cancelled", "failed"}),
        "sending": frozenset({"sent", "failed", "unknown", "cancelled"}),
        "sent": frozenset(),
        "failed": frozenset({"pending", "cancelled"}),
        "unknown": frozenset(),
        "cancelled": frozenset(),
    },
}


def validate_transition(machine: str, current: str, target: str) -> None:
    states = TRANSITIONS.get(machine)
    if states is None:
        raise StateTransitionError(f"unknown state machine: {machine}")
    if current not in states:
        raise StateTransitionError(f"unknown {machine} state: {current}")
    if target not in states[current]:
        raise StateTransitionError(f"invalid {machine} transition: {current} -> {target}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanIdentity:
    followup_id: str
    operation_id: str
    operation_version: str
    parameters: dict[str, Any]
    effect_class: str
    evidence_revision: int
    permission_revision: int

    @property
    def parameters_hash(self) -> str:
        return canonical_hash(self.parameters)

    @property
    def plan_hash(self) -> str:
        return canonical_hash(
            {
                "effect_class": self.effect_class,
                "evidence_revision": self.evidence_revision,
                "followup_id": self.followup_id,
                "operation_id": self.operation_id,
                "operation_version": self.operation_version,
                "parameters": self.parameters,
                "permission_revision": self.permission_revision,
            }
        )

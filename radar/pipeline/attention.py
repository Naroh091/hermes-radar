"""Deterministic attention policy (ported from mail-intelligence): code applies rules, the model proposes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..preferences import applicable

DISPOSITIONS = ("notify_now", "digest", "silent")
PROTECTED = ("security", "service_interruption", "deadline", "financial_loss")


@dataclass(frozen=True)
class Decision:
    disposition: str
    reason: str
    applied_rules: tuple = field(default_factory=tuple)
    overridden_rules: tuple = field(default_factory=tuple)
    conflicts: tuple = field(default_factory=tuple)


def risk_flags_of(value: Any) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(flag) for flag in value if flag in PROTECTED)


def resolve(
    observation: dict[str, Any], preferences: list[dict[str, Any]], *, model_decision: str,
    risk_flags: Any = (), info_complete: bool = True, muted: bool = False,
) -> Decision:
    """Final disposition. Ambiguity resolves toward notifying; a muted task is always silent."""
    if model_decision not in DISPOSITIONS:
        raise ValueError("model_decision must be notify_now, digest or silent")
    if muted:
        return Decision("silent", "task_muted")
    flags = risk_flags_of(risk_flags)
    matched = applicable(preferences, observation)
    ids = tuple(p["id"] for p in matched)
    protected = set(PROTECTED) & set(flags)
    if protected:
        overriding = tuple(p["id"] for p in matched if p.get("effect") == "silent")
        return Decision("notify_now", "protected_exception", applied_rules=ids, overridden_rules=overriding)
    if not info_complete and model_decision == "silent":
        return Decision("digest", "incomplete_information", applied_rules=ids)
    silence = [p for p in matched if p.get("effect") == "silent" and not (set(p.get("exceptions") or []) & set(flags))]
    louder = [p for p in matched if p.get("effect") in ("notify", "prefer")]
    digest = [p for p in matched if p.get("effect") == "digest"]
    conflict = tuple(p["id"] for p in silence + louder + digest) if silence and (louder or digest) else ()
    if louder:
        return Decision("notify_now", "preference_notify", applied_rules=ids, conflicts=conflict)
    if digest:
        return Decision("digest", "preference_digest", applied_rules=(digest[0]["id"],), conflicts=conflict)
    if silence:
        return Decision("silent", "preference_silence", applied_rules=(silence[0]["id"],))
    return Decision(model_decision, "model_evaluation")

"""Rule-based triage of one signal: keep for extraction or ignore with a reason."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..preferences import applicable, subject_stem
from . import rules


@dataclass(frozen=True)
class TriageDecision:
    keep: bool
    reason: str
    priority: str = "normal"


def _observation(signal: dict[str, Any]) -> dict[str, Any]:
    meta = signal.get("meta") or {}
    return {
        "account": meta.get("account", ""),
        "sender": meta.get("sender", ""),
        "message_type": signal.get("kind", ""),
        "project": meta.get("project", ""),
        "kind": signal.get("kind", ""),
        "source_kind": meta.get("source_kind", signal.get("kind", "")),
        "repo": meta.get("repo", ""),
        "subject_stem": subject_stem(meta.get("subject") or signal.get("title") or ""),
    }


def triage_signal(signal: dict[str, Any], preferences: list[dict[str, Any]], config: dict[str, Any] | None = None) -> TriageDecision:
    kind = signal.get("kind", "")
    meta = signal.get("meta") or {}
    for pref in applicable(preferences, _observation(signal)):
        if pref.get("effect") == "silent" and not pref.get("exceptions"):
            return TriageDecision(False, f"preference:{pref.get('id')}")
    if kind == "email":
        subject = signal.get("title", "")
        text = f"{subject}\n{signal.get('snippet', '')}"
        transactional = rules.is_transactional(text)
        signals = set(meta.get("automation") or [])
        if rules.subject_is_noise(subject) and not transactional:
            return TriageDecision(False, "noise_subject")
        if "list-unsubscribe" in signals and not transactional:
            return TriageDecision(False, "bulk_no_signal")
        if signals & rules.STRONG_AUTOMATION and not transactional and not meta.get("thread_reply"):
            return TriageDecision(False, "automated_no_signal")
        return TriageDecision(True, "email", "normal" if transactional else "low")
    if kind == "session_turn":
        user_text = str(meta.get("user_text") or signal.get("snippet") or "")
        if user_text.lstrip().startswith("/"):
            return TriageDecision(False, "command")
        if len(user_text.strip()) < rules.MIN_TURN_CHARS:
            return TriageDecision(False, "short_turn")
        if meta.get("radar_origin"):
            return TriageDecision(False, "own_prompt")
        return TriageDecision(True, "session_turn", "normal" if rules.is_tasky_turn(user_text) else "low")
    if kind == "github":
        actor = str(meta.get("actor") or "")
        if actor.endswith("[bot]") and meta.get("event_type") not in {"workflow_run", "deployment_status"}:
            return TriageDecision(False, "bot_actor")
        return TriageDecision(True, "github", "normal" if meta.get("event_type") in {"pull_request", "issues", "workflow_run", "deployment_status", "pull_request_review"} else "low")
    if kind in {"calendar", "job_result", "cron_result"}:
        return TriageDecision(True, kind)
    return TriageDecision(True, "default", "low")


def triage_bundle(signals: list[dict[str, Any]], preferences: list[dict[str, Any]], config: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], TriageDecision]]]:
    """Split a bundle into signals worth a model call and ignored ones (with reasons)."""
    kept: list[dict[str, Any]] = []
    ignored: list[tuple[dict[str, Any], TriageDecision]] = []
    for signal in signals:
        decision = triage_signal(signal, preferences, config)
        if decision.keep:
            kept.append(signal)
        else:
            ignored.append((signal, decision))
    return kept, ignored

from __future__ import annotations

from datetime import datetime, timezone

from proactive.detectors import detect

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


def test_explicit_commitment_requires_both_commitment_and_time():
    candidates = detect(
        "llm_call",
        {"turn_id": "turn-1", "assistant_response": "Lo comprobaré en 2 horas y te avisaré."},
        NOW,
    )
    assert len(candidates) == 1
    assert candidates[0].detector == "explicit_commitment"
    assert candidates[0].next_check_at.isoformat() == "2026-09-16T12:00:00+00:00"
    assert detect("llm_call", {"assistant_response": "Lo comprobaré."}, NOW) == []


def test_deadline_and_missing_input_detectors_are_conservative():
    deadline = detect(
        "llm_call",
        {"turn_id": "turn-2", "user_message": "Recuérdame el plazo mañana."},
        NOW,
    )
    missing = detect(
        "tool_call",
        {"turn_id": "turn-3", "tool_name": "publish", "status": "failed", "error_type": "ApprovalRequired"},
        NOW,
    )
    ignored = detect(
        "tool_call",
        {"turn_id": "turn-4", "tool_name": "publish", "status": "failed", "error_type": "TimeoutError"},
        NOW,
    )
    assert [item.detector for item in deadline] == ["deadline"]
    assert [item.detector for item in missing] == ["missing_input"]
    assert ignored == []

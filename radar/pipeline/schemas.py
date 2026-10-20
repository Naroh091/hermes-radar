"""Closed JSON schemas for every model step. Every property is required so strict providers accept them."""
from __future__ import annotations

CATEGORIES = ["trip", "purchase", "work_item", "commitment", "check", "routine", "other", "noise"]
TASK_KINDS = ["trip", "purchase", "work_item", "commitment", "check", "routine", "other"]
DISPOSITIONS = ["notify_now", "digest", "silent"]
URGENCIES = ["now", "today", "this_week", "whenever"]
RISK_FLAGS = ["security", "service_interruption", "deadline", "financial_loss"]
ACTION_CLASSES = ["read", "local", "external"]
SCOPE_FIELDS = ["account", "sender", "message_type", "project", "kind", "source_kind", "repo"]


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": list(required if required is not None else properties.keys()),
        "properties": properties,
    }


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


_STR = {"type": "string"}
_NUM = {"type": "number"}
_BOOL = {"type": "boolean"}

ENTITIES_SCHEMA = _obj(
    {
        "dates": _arr(_obj({"label": _STR, "when": _STR})),
        "places": _arr(_STR),
        "amounts": _arr(_obj({"label": _STR, "value": _NUM, "currency": _STR})),
        "people": _arr(_STR),
        "organizations": _arr(_STR),
        "identifiers": _arr(_obj({"label": _STR, "value": _STR})),
        "items": _arr(_STR),
        "repo": _STR,
        "url": _STR,
    }
)

EXTRACT_SCHEMA = _obj(
    {
        "relevant": _BOOL,
        "category": {"type": "string", "enum": CATEGORIES},
        "title": _STR,
        "summary": _STR,
        "disposition": {"type": "string", "enum": DISPOSITIONS},
        "reason": _STR,
        "urgency": {"type": "string", "enum": ["high", "medium", "low"]},
        "impact": {"type": "string", "enum": ["high", "medium", "low", "none"]},
        "risk_flags": _arr({"type": "string", "enum": RISK_FLAGS}),
        "entities": ENTITIES_SCHEMA,
        "expectations": _arr(_obj({
            "description": _STR,
            "kind": {"type": "string", "enum": ["email", "event", "date", "repo", "other"]},
            "by": _STR,
        })),
        "facts_learned": _arr(_obj({"key": _STR, "value": _STR, "confidence": _NUM})),
        "proposed_reply": _STR,
        "keep_full_content": _BOOL,
        "needs_thread": _BOOL,
        "conversation_state": {"type": "string", "enum": ["resolved", "open", "deferred", "not_a_conversation"]},
    }
)

PLAN_SCHEMA = _obj(
    {
        "task_match": _obj({
            "decision": {"type": "string", "enum": ["new", "advances", "completes", "contradicts", "none"]},
            "task_id": _STR,
            "why": _STR,
        }),
        "task": _obj({
            "kind": {"type": "string", "enum": TASK_KINDS},
            "title": _STR,
            "summary": _STR,
            "status": {"type": "string", "enum": ["open", "waiting", "needs_user", "in_progress", "done", "abandoned"]},
            "due_at": _STR,
            "next_review_at": _STR,
            "confidence": _NUM,
        }),
        "checklist": _arr(_obj({
            "item": _STR,
            "status": {"type": "string", "enum": ["done", "missing", "unknown", "not_applicable"]},
            "note": _STR,
        })),
        "checks": _arr(_obj({
            "description": _STR,
            "severity": {"type": "string", "enum": ["info", "warning", "high"]},
            "ask_user": _BOOL,
        })),
        "proposals": _arr(_obj({
            "title": _STR,
            "rationale": _STR,
            "action_class": {"type": "string", "enum": ACTION_CLASSES},
            "operation": _STR,
            "when": _STR,
            "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
            "options": _arr(_obj({"id": _STR, "label": _STR})),
        })),
        "notify": _obj({
            "disposition": {"type": "string", "enum": DISPOSITIONS},
            "urgency": {"type": "string", "enum": URGENCIES},
            "importance": {"type": "string", "enum": ["high", "normal", "low"]},
            "reason": _STR,
        }),
        "message": _STR,
        "escalation": _obj({"decision": {"type": "string", "enum": ["handle", "inform", "ask"]}, "why": _STR}),
    }
)

INTENT_SCHEMA = _obj(
    {
        "intent": {"type": "string", "enum": ["approve", "reject", "edit_request", "preference", "question", "thanks", "later", "unclear"]},
        "proposal_index": {"type": "integer"},
        "reason": _STR,
    }
)


PREFERENCE_SCHEMA = _obj(
    {
        "applies": _BOOL,
        "dimension": {"type": "string", "enum": ["notification", "investigation", "response"]},
        "effect": {"type": "string", "enum": ["silent", "digest", "notify", "prefer"]},
        "scope": _obj({key: _STR for key in SCOPE_FIELDS}),
        "exceptions": _arr({"type": "string", "enum": RISK_FLAGS}),
        "instruction": _STR,
        "why": _STR,
    }
)


PATTERN_SCHEMA = _obj(
    {
        "patterns": _arr(_obj({
            "fingerprint": _STR,
            "name": _STR,
            "description": _STR,
            "worth_automating": _BOOL,
            "schedule_cron": _STR,
            "prompt": _STR,
            "rationale": _STR,
            "confidence": _NUM,
        })),
    }
)

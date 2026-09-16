from __future__ import annotations

from typing import Any


def _schema(required: list[str], properties: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


_STRING = {"type": "string", "minLength": 1}
_INTEGER = {"type": "integer", "minimum": 1}
_OBJECT = {"type": "object", "additionalProperties": False}

DOMAIN_SCHEMAS: dict[str, dict[str, Any]] = {
    "source": _schema(
        ["kind", "account_ref", "resource_scope", "enabled", "permission_revision", "retention_days"],
        {
            "kind": _STRING,
            "account_ref": _STRING,
            "resource_scope": {"type": "object"},
            "enabled": {"type": "boolean"},
            "permission_revision": _INTEGER,
            "cursor": {"type": "object"},
            "retention_days": _INTEGER,
        },
    ),
    "grant": _schema(
        ["permission", "operation_ids", "resource_scope", "limits"],
        {
            "permission": {"enum": ["observe", "read", "notify"]},
            "source_id": {"type": ["string", "null"]},
            "operation_ids": {"type": "array", "items": _STRING, "uniqueItems": True},
            "resource_scope": {"type": "object"},
            "destination_id": {"type": ["string", "null"]},
            "limits": {"type": "object"},
            "expires_at": {"type": ["string", "null"]},
        },
    ),
    "observation": _schema(
        ["source_id", "source_event_key", "occurred_at", "received_at", "kind", "fingerprint", "facts", "origin"],
        {
            "source_id": _STRING,
            "source_event_key": _STRING,
            "occurred_at": _STRING,
            "received_at": _STRING,
            "kind": _STRING,
            "fingerprint": _STRING,
            "facts": {"type": "object"},
            "expires_at": {"type": ["string", "null"]},
            "origin": {"enum": ["user", "tool", "external", "plugin"]},
            "correlation_id": {"type": ["string", "null"]},
        },
    ),
    "evidence": _schema(
        ["observation_id", "source_ref", "excerpt_redacted", "epistemic_status"],
        {
            "observation_id": _STRING,
            "source_ref": _STRING,
            "excerpt_redacted": {"type": "string"},
            "epistemic_status": {"enum": ["documented", "user_stated", "inferred", "disputed", "retracted"]},
            "supersedes_id": {"type": ["string", "null"]},
            "expires_at": {"type": ["string", "null"]},
        },
    ),
    "goal": _schema(
        ["title", "status", "scope"],
        {
            "title": _STRING,
            "status": {"enum": ["active", "paused", "completed", "cancelled"]},
            "scope": {"type": "object"},
            "user_intent_ref": {"type": ["string", "null"]},
            "parent_goal_id": {"type": ["string", "null"]},
        },
    ),
    "followup": _schema(
        ["goal_id", "reason", "status", "subject_key", "evidence_revision", "expires_at", "timezone", "confidence_band", "assumptions", "stop_rule"],
        {
            "goal_id": _STRING,
            "reason": {"enum": ["condition_wait", "missing_input", "next_step", "agent_commitment", "routine", "external_signal"]},
            "status": {"enum": ["candidate", "ready", "waiting_input", "waiting_event", "suppressed", "checking", "awaiting_consent", "completed", "failed", "paused", "cancelled", "expired"]},
            "subject_key": _STRING,
            "evidence_revision": _INTEGER,
            "next_check_at": {"type": ["string", "null"]},
            "deadline_at": {"type": ["string", "null"]},
            "expires_at": _STRING,
            "timezone": _STRING,
            "confidence_band": {"enum": ["low", "medium", "high"]},
            "assumptions": {"type": "object"},
            "stop_rule": {"type": "object"},
        },
    ),
    "plan": _schema(
        ["operation_id", "operation_version", "parameters", "effect_class", "evidence_revision", "permission_revision"],
        {
            "operation_id": _STRING,
            "operation_version": _STRING,
            "parameters": {"type": "object"},
            "effect_class": {"enum": ["informational", "mutation"]},
            "evidence_revision": _INTEGER,
            "permission_revision": _INTEGER,
        },
    ),
    "proposal": _schema(
        ["followup_id", "plan_id", "destination_id", "status", "expires_at", "priority", "reason_text", "suppression_key"],
        {
            "followup_id": _STRING,
            "plan_id": _STRING,
            "destination_id": _STRING,
            "status": {"enum": ["draft", "pending", "accepted", "rejected", "expired", "cancelled", "executing", "succeeded", "failed", "unknown"]},
            "expires_at": _STRING,
            "priority": {"enum": ["normal", "low", "deadline"]},
            "reason_text": _STRING,
            "suppression_key": _STRING,
        },
    ),
    "consent": _schema(
        ["proposal_id", "plan_hash", "decision", "actor_ref", "verified_transport", "destination_id", "expires_at", "token_hash"],
        {
            "proposal_id": _STRING,
            "plan_hash": _STRING,
            "decision": {"enum": ["approved", "rejected"]},
            "actor_ref": _STRING,
            "verified_transport": _STRING,
            "destination_id": _STRING,
            "expires_at": _STRING,
            "token_hash": _STRING,
        },
    ),
    "run": _schema(
        ["followup_id", "plan_id", "effect_class", "status", "execution_key"],
        {
            "followup_id": _STRING,
            "plan_id": _STRING,
            "consent_id": {"type": ["string", "null"]},
            "effect_class": {"enum": ["informational", "mutation"]},
            "status": {"enum": ["queued", "running", "succeeded", "failed", "unknown", "cancelled"]},
            "execution_key": _STRING,
        },
    ),
    "destination": _schema(
        ["platform", "gateway_connection_ref", "address", "owner_platform_id", "kind", "capabilities", "verified_at", "enabled"],
        {
            "platform": _STRING,
            "gateway_connection_ref": _STRING,
            "address": {"type": "object"},
            "owner_platform_id": _STRING,
            "kind": {"enum": ["private", "group"]},
            "capabilities": {"type": "object"},
            "verified_at": _STRING,
            "enabled": {"type": "boolean"},
        },
    ),
    "outbox": _schema(
        ["destination_id", "notification_key", "payload", "status", "next_attempt_at"],
        {
            "destination_id": _STRING,
            "followup_id": {"type": ["string", "null"]},
            "proposal_id": {"type": ["string", "null"]},
            "notification_key": _STRING,
            "payload": {"type": "object"},
            "status": {"enum": ["pending", "sending", "sent", "failed", "unknown", "cancelled"]},
            "next_attempt_at": _STRING,
        },
    ),
}


def validate_closed_object(schema_name: str, value: Any) -> tuple[bool, list[str]]:
    schema = DOMAIN_SCHEMAS.get(schema_name)
    if schema is None:
        return False, [f"unknown schema: {schema_name}"]
    if not isinstance(value, dict):
        return False, ["value must be an object"]
    unknown = sorted(set(value) - set(schema["properties"]))
    if unknown:
        return False, ["unknown fields: " + ", ".join(unknown)]
    missing = sorted(set(schema["required"]) - set(value))
    if missing:
        return False, ["missing fields: " + ", ".join(missing)]
    return True, []

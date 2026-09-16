"""Closed schemas and local validation for the plugin configuration."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


CONFIG_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "mode": "shadow",
    "owner": {"id": None, "timezone": "UTC", "language": "es"},
    "destination": {
        "platform": None,
        "gateway_connection_ref": None,
        "resolution": "explicit",
        "address": {},
        "owner_platform_id": None,
        "allow_groups": False,
        "verified": False,
    },
    "observation": {"local_sessions": False, "historical_import": False},
    "scheduler": {"tick_seconds": 60, "max_concurrent_checks": 2},
    "notifications": {
        "enabled": False,
        "quiet_start": "22:00",
        "quiet_end": "08:00",
        "daily_limit": 3,
        "batch_minutes": 30,
        "reminder_cooldown_hours": 24,
    },
    "limits": {
        "events_per_source_cycle": 100,
        "checks_per_day": 200,
        "llm_calls_per_day": 20,
        "llm_input_tokens": 2000,
        "llm_output_tokens": 500,
    },
    "retention": {"observations_days": 30, "results_days": 30, "audit_days": 90},
    "sources": [],
    "allowed_operations": [],
    "mutations": {"enabled": False, "require_per_action_consent": True},
}

_NESTED_KEYS = {
    "owner": {"id", "timezone", "language"},
    "destination": {"platform", "gateway_connection_ref", "resolution", "address", "owner_platform_id", "allow_groups", "verified"},
    "observation": {"local_sessions", "historical_import"},
    "scheduler": {"tick_seconds", "max_concurrent_checks"},
    "notifications": {"enabled", "quiet_start", "quiet_end", "daily_limit", "batch_minutes", "reminder_cooldown_hours"},
    "limits": {"events_per_source_cycle", "checks_per_day", "llm_calls_per_day", "llm_input_tokens", "llm_output_tokens"},
    "retention": {"observations_days", "results_days", "audit_days"},
    "mutations": {"enabled", "require_per_action_consent"},
}


def defaults() -> dict[str, Any]:
    return deepcopy(CONFIG_DEFAULTS)


def validate_config(value: Any) -> tuple[bool, list[str]]:
    if not isinstance(value, dict):
        return False, ["configuration must be an object"]
    errors: list[str] = []
    unknown = sorted(set(value) - set(CONFIG_DEFAULTS))
    if unknown:
        errors.append("unknown keys: " + ", ".join(unknown))
    if "enabled" in value and not isinstance(value["enabled"], bool):
        errors.append("enabled must be boolean")
    if value.get("mode", "shadow") not in {"shadow", "active"}:
        errors.append("mode must be shadow or active")

    for key, allowed in _NESTED_KEYS.items():
        nested = value.get(key)
        if not isinstance(nested, dict):
            errors.append(f"{key} must be object")
            continue
        nested_unknown = sorted(set(nested) - allowed)
        if nested_unknown:
            errors.append(f"{key} has unknown keys: " + ", ".join(nested_unknown))

    for key in ("sources", "allowed_operations"):
        if not isinstance(value.get(key), list):
            errors.append(f"{key} must be array")
    if isinstance(value.get("allowed_operations"), list) and any(
        not isinstance(item, str) or not item for item in value["allowed_operations"]
    ):
        errors.append("allowed_operations must contain non-empty strings")
    sources = value.get("sources")
    if isinstance(sources, list):
        seen_source_ids: set[str] = set()
        allowed_source_keys = {
            "id", "kind", "url", "interval_seconds", "retention_days", "enabled", "operations"
        }
        for index, source in enumerate(sources):
            prefix = f"sources[{index}]"
            if not isinstance(source, dict):
                errors.append(f"{prefix} must be object")
                continue
            unknown_source = sorted(set(source) - allowed_source_keys)
            if unknown_source:
                errors.append(f"{prefix} has unknown keys: " + ", ".join(unknown_source))
            source_id = source.get("id")
            if not isinstance(source_id, str) or not source_id.strip():
                errors.append(f"{prefix}.id is required")
            elif source_id in seen_source_ids:
                errors.append(f"{prefix}.id must be unique")
            else:
                seen_source_ids.add(source_id)
            if source.get("kind") != "http_status":
                errors.append(f"{prefix}.kind must be http_status")
            if not isinstance(source.get("url"), str) or not source["url"].startswith("https://"):
                errors.append(f"{prefix}.url must be an HTTPS URL")
            for field in ("interval_seconds", "retention_days"):
                if field in source and (
                    isinstance(source[field], bool) or not isinstance(source[field], int) or source[field] < 1
                ):
                    errors.append(f"{prefix}.{field} must be a positive integer")
            if "enabled" in source and not isinstance(source["enabled"], bool):
                errors.append(f"{prefix}.enabled must be boolean")
            operations = source.get("operations", ["http_status.check"])
            if not isinstance(operations, list) or any(not isinstance(op, str) or not op for op in operations):
                errors.append(f"{prefix}.operations must contain non-empty strings")

    owner = value.get("owner")
    if value.get("enabled") is True and (
        not isinstance(owner, dict)
        or not isinstance(owner.get("id"), str)
        or not owner["id"].strip()
    ):
        errors.append("owner.id is required when enabled")

    destination = value.get("destination")
    if isinstance(destination, dict):
        if destination.get("verified") is True:
            errors.append("destination.verified is server-managed and cannot be true in configuration")
        if not isinstance(destination.get("address", {}), dict):
            errors.append("destination.address must be object")
        if destination.get("allow_groups", False) not in (True, False):
            errors.append("destination.allow_groups must be boolean")
        if destination.get("platform") not in (None, "telegram"):
            errors.append("destination.platform must be null or telegram")

    for section, fields in {
        "scheduler": ("tick_seconds", "max_concurrent_checks"),
        "notifications": ("daily_limit", "batch_minutes", "reminder_cooldown_hours"),
        "limits": ("events_per_source_cycle", "checks_per_day", "llm_calls_per_day", "llm_input_tokens", "llm_output_tokens"),
        "retention": ("observations_days", "results_days", "audit_days"),
    }.items():
        nested = value.get(section)
        if not isinstance(nested, dict):
            continue
        for field in fields:
            number = nested.get(field)
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                errors.append(f"{section}.{field} must be a positive integer")

    notifications = value.get("notifications")
    if isinstance(notifications, dict) and not isinstance(notifications.get("enabled"), bool):
        errors.append("notifications.enabled must be boolean")
    if isinstance(notifications, dict):
        for field in ("quiet_start", "quiet_end"):
            raw = notifications.get(field)
            valid_time = False
            if isinstance(raw, str) and len(raw) == 5 and raw[2] == ":":
                try:
                    hour, minute = map(int, raw.split(":"))
                    valid_time = 0 <= hour <= 23 and 0 <= minute <= 59
                except ValueError:
                    pass
            if not valid_time:
                errors.append(f"notifications.{field} must be HH:MM")
        if notifications.get("enabled") is True:
            if value.get("mode") != "active":
                errors.append("notifications require active mode")
            if not isinstance(destination, dict) or destination.get("platform") is None:
                errors.append("notifications require destination.platform")
            if not isinstance(destination, dict) or not isinstance(destination.get("owner_platform_id"), str) or not destination["owner_platform_id"]:
                errors.append("notifications require destination.owner_platform_id")
    mutations = value.get("mutations")
    if isinstance(mutations, dict):
        if not isinstance(mutations.get("enabled"), bool):
            errors.append("mutations.enabled must be boolean")
        if mutations.get("require_per_action_consent") is not True:
            errors.append("mutations.require_per_action_consent must remain true")

    return not errors, errors

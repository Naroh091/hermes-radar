"""Schemas y validación local del plugin."""
from __future__ import annotations

from typing import Any

CONFIG_DEFAULTS = {
    "enabled": False, "mode": "shadow", "owner": {}, "destination": {},
    "observation": {}, "scheduler": {}, "notifications": {}, "limits": {},
    "retention": {}, "sources": [], "allowed_operations": [], "mutations": {},
}


def validate_config(value: Any) -> tuple[bool, list[str]]:
    if not isinstance(value, dict):
        return False, ["configuration must be an object"]
    errors = []
    unknown = set(value) - set(CONFIG_DEFAULTS)
    if unknown:
        errors.append("unknown keys: " + ", ".join(sorted(unknown)))
    if "enabled" in value and not isinstance(value["enabled"], bool):
        errors.append("enabled must be boolean")
    if value.get("mode", "shadow") not in {"shadow", "active"}:
        errors.append("mode must be shadow or active")
    for key in ("owner", "destination", "observation", "scheduler", "notifications", "limits", "retention", "mutations"):
        if key in value and not isinstance(value[key], dict): errors.append(f"{key} must be object")
    for key in ("sources", "allowed_operations"):
        if key in value and not isinstance(value[key], list): errors.append(f"{key} must be array")
    return not errors, errors

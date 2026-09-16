from __future__ import annotations

from copy import deepcopy
from typing import Any

from .schemas import CONFIG_DEFAULTS, validate_config


def _merge(default: Any, configured: Any) -> Any:
    if isinstance(default, dict) and isinstance(configured, dict):
        merged = deepcopy(default)
        for key, value in configured.items():
            merged[key] = _merge(default.get(key), value) if key in default else deepcopy(value)
        return merged
    return deepcopy(configured)


def load_config(ctx) -> dict[str, Any]:
    """Read plugin-relative settings and merge partial sections into safe defaults."""
    merged = deepcopy(CONFIG_DEFAULTS)
    for key, default in CONFIG_DEFAULTS.items():
        configured = ctx.get_config(key, default=deepcopy(default))
        merged[key] = _merge(default, configured)
    ok, errors = validate_config(merged)
    if not ok:
        raise ValueError("invalid proactive configuration: " + "; ".join(errors))
    return merged


def defaults() -> dict[str, Any]:
    return deepcopy(CONFIG_DEFAULTS)

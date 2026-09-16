from __future__ import annotations

from copy import deepcopy
from typing import Any
from .schemas import CONFIG_DEFAULTS, validate_config


def load_config(ctx) -> dict[str, Any]:
    # PluginContext exposes only plugin-relative paths; read each top-level setting.
    merged = deepcopy(CONFIG_DEFAULTS)
    for key, default in CONFIG_DEFAULTS.items():
        value = ctx.get_config(key, default=default)
        if value != default or key not in {"owner", "destination", "observation", "scheduler", "notifications", "limits", "retention", "mutations"}:
            merged[key] = value
    ok, errors = validate_config(merged)
    if not ok: raise ValueError("invalid proactive configuration: " + "; ".join(errors))
    return merged


def defaults() -> dict[str, Any]: return deepcopy(CONFIG_DEFAULTS)

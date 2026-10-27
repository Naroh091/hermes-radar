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


def _normalize(merged: dict[str, Any]) -> None:
    """Repair YAML quirks that are not configuration errors (``22:00`` parses as the integer 1320)."""
    notifications = merged.get("notifications")
    if isinstance(notifications, dict):
        for flat, field in (("notices_per_day", "daily_limit"), ("reminders_per_day", "reminders_per_day"),
                            ("reminder_gap_minutes", "reminder_gap_minutes"), ("quiet_start", "quiet_start"), ("quiet_end", "quiet_end")):
            value = merged.get(flat)
            if value is not None and not isinstance(value, bool):
                notifications[field] = value  # flat keys (Desktop form) win over the nested section
        for field in ("quiet_start", "quiet_end"):
            value = notifications.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 24 * 60:
                notifications[field] = f"{value // 60:02d}:{value % 60:02d}"
    level = merged.get("trust_level")
    if isinstance(level, str) and level in ("cautious", "balanced", "autonomous") and isinstance(merged.get("trust"), dict):
        merged["trust"]["level"] = level  # the flat key (Desktop form, /radar trust) wins over trust.level
    for flat, section, field in (
        ("jobs_per_day", "limits", "jobs_per_day"),
        ("auto_jobs_per_task", "jobs", "max_auto_per_task"),
        ("auto_job_cooldown_minutes", "jobs", "auto_cooldown_minutes"),
    ):
        value = merged.get(flat)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if isinstance(merged.get(section), dict):
            merged[section][field] = value  # flat keys (Desktop form) win over the nested sections
    destination = merged.get("destination")
    if isinstance(destination, dict) and isinstance(destination.get("address"), dict):
        for field in ("chat_id", "thread_id"):
            value = destination["address"].get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                destination["address"][field] = str(value)
        if isinstance(destination.get("owner_platform_id"), int):
            destination["owner_platform_id"] = str(destination["owner_platform_id"])


def load_config(ctx) -> dict[str, Any]:
    """Read plugin-relative settings and merge partial sections into safe defaults."""
    merged = deepcopy(CONFIG_DEFAULTS)
    for key, default in CONFIG_DEFAULTS.items():
        configured = ctx.get_config(key, default=deepcopy(default))
        merged[key] = _merge(default, configured)
    _normalize(merged)
    ok, errors = validate_config(merged)
    if not ok:
        raise ValueError("invalid radar configuration: " + "; ".join(errors))
    return merged


def defaults() -> dict[str, Any]:
    return deepcopy(CONFIG_DEFAULTS)

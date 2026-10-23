"""Closed schemas and local validation for the plugin configuration."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

SOURCE_KINDS = ("http_status", "gmail", "calendar", "github")
TRUST_LEVELS = ("cautious", "balanced", "autonomous")

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
    "observation": {"local_sessions": False, "historical_import": False, "state_db_poll_seconds": 0, "debounce_seconds": 120, "session_quiet_minutes": 30,
                    "session_auto_jobs": False},
    "context": {"memory": True, "sessions": True, "timeout_seconds": 45, "max_chars": 1500, "session_results": 3, "memory_tool": ""},
    "scheduler": {"tick_seconds": 60, "max_concurrent_checks": 2},
    "notifications": {
        "enabled": False,
        "quiet_start": "22:00",
        "quiet_end": "08:00",
        "daily_limit": 3,
        "batch_minutes": 30,
        "digest_times": ["09:00", "14:00", "19:00"],
        "reminder_cooldown_hours": 24,
        "reminder_items": 1,
        "unanswered_hours": 48,
        "interrupt_cooldown_hours": 3,
    },
    "limits": {
        "events_per_source_cycle": 100,
        "checks_per_day": 200,
        "llm_calls_per_day": 200,
        "llm_input_tokens": 2000,
        "llm_output_tokens": 500,
        "jobs_per_day": 100,
        "max_bundles_per_tick": 5,
        "signal_attempts": 5,
    },
    "retention": {
        "observations_days": 30, "results_days": 30, "audit_days": 90, "snippet_chars": 500,
        "full_content_days": 14, "full_content_kinds": ["work_item"], "signals_days": 90,
    },
    "sources": [],
    "allowed_operations": [],
    "mutations": {"enabled": False, "require_per_action_consent": True},
    "trust": {"level": "balanced", "overrides": {}, "auto_external": []},
    # Flat mirror of trust.level so the Desktop settings form can render it as a selector.
    "trust_level": None,
    # Flat keys for the Desktop settings form; when set they override limits.jobs_per_day,
    # jobs.max_auto_per_task and jobs.auto_cooldown_minutes.
    "jobs_per_day": None,
    "auto_jobs_per_task": None,
    "auto_job_cooldown_minutes": None,
    "notices_per_day": None,
    "quiet_start": None,
    "quiet_end": None,
    "ingress": {
        "gmail_receiver": {
            "enabled": False, "host": "127.0.0.1", "port": 8666, "public_url": "", "route": "gmail-push",
            "hermes_webhook_url": "http://127.0.0.1:8644/webhooks/gmail-push", "require_oidc": True,
            "service_account_email": "", "expected_subscription": "",
        }
    },
    "jobs": {"enabled": True, "max_concurrent": 2, "max_turns": 20, "max_seconds": 300, "model": None, "worktree": True, "skills": [], "toolsets": [],
             "max_auto_per_task": 3, "auto_cooldown_minutes": 15},
    "workspaces": {},
    "desktop": {"panel_label": "Radar"},
}

_NESTED_KEYS = {
    "owner": {"id", "timezone", "language"},
    "destination": {"platform", "gateway_connection_ref", "resolution", "address", "owner_platform_id", "allow_groups", "verified"},
    "observation": {"local_sessions", "historical_import", "state_db_poll_seconds", "debounce_seconds", "session_quiet_minutes", "session_auto_jobs"},
    "context": {"memory", "sessions", "timeout_seconds", "max_chars", "session_results", "memory_tool"},
    "scheduler": {"tick_seconds", "max_concurrent_checks"},
    "notifications": {"enabled", "quiet_start", "quiet_end", "daily_limit", "batch_minutes", "digest_times", "reminder_cooldown_hours",
                      "reminder_items", "unanswered_hours", "interrupt_cooldown_hours"},
    "limits": {"events_per_source_cycle", "checks_per_day", "llm_calls_per_day", "llm_input_tokens", "llm_output_tokens", "jobs_per_day", "max_bundles_per_tick", "signal_attempts"},
    "retention": {"observations_days", "results_days", "audit_days", "snippet_chars", "full_content_days", "full_content_kinds", "signals_days"},
    "mutations": {"enabled", "require_per_action_consent"},
    "trust": {"level", "overrides", "auto_external"},
    "ingress": {"gmail_receiver"},
    "jobs": {"enabled", "max_concurrent", "max_turns", "max_seconds", "model", "worktree", "skills", "toolsets", "max_auto_per_task", "auto_cooldown_minutes"},
    "desktop": {"panel_label"},
}
_RECEIVER_KEYS = {"enabled", "host", "port", "public_url", "route", "hermes_webhook_url", "require_oidc", "service_account_email", "expected_subscription"}
_SOURCE_COMMON = {"id", "kind", "enabled", "interval_seconds", "retention_days"}
_SOURCE_KEYS = {
    "http_status": _SOURCE_COMMON | {"url", "operations"},
    "gmail": _SOURCE_COMMON | {"address", "provider", "server", "labels", "push", "initial_lookback_hours", "mapping"},
    "calendar": _SOURCE_COMMON | {"provider", "server", "calendar_id", "account", "lookback_hours", "lookahead_days", "initial_import"},
    "github": _SOURCE_COMMON | {"provider", "repos", "events", "route", "consume", "all_branches", "initial_import"},
}
_PUSH_KEYS = {"enabled", "topic", "credentials_path"}


def defaults() -> dict[str, Any]:
    return deepcopy(CONFIG_DEFAULTS)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_source(index: int, source: Any, errors: list[str], seen: set[str], allowed_operations: list[str]) -> None:
    prefix = f"sources[{index}]"
    if not isinstance(source, dict):
        errors.append(f"{prefix} must be object")
        return
    kind = source.get("kind")
    if kind not in SOURCE_KINDS:
        errors.append(f"{prefix}.kind must be one of {', '.join(SOURCE_KINDS)}")
        return
    unknown_source = sorted(set(source) - _SOURCE_KEYS[kind])
    if unknown_source:
        errors.append(f"{prefix} has unknown keys: " + ", ".join(unknown_source))
    source_id = source.get("id")
    if not isinstance(source_id, str) or not source_id.strip():
        errors.append(f"{prefix}.id is required")
    elif source_id in seen:
        errors.append(f"{prefix}.id must be unique")
    else:
        seen.add(source_id)
    for field in ("interval_seconds", "retention_days", "initial_lookback_hours"):
        if field in source and not _positive_int(source[field]):
            errors.append(f"{prefix}.{field} must be a positive integer")
    if "enabled" in source and not isinstance(source["enabled"], bool):
        errors.append(f"{prefix}.enabled must be boolean")
    if kind == "http_status":
        if not isinstance(source.get("url"), str) or not source["url"].startswith("https://"):
            errors.append(f"{prefix}.url must be an HTTPS URL")
        operations = source.get("operations", ["http_status.check"])
        if not isinstance(operations, list) or any(not isinstance(op, str) or not op for op in operations):
            errors.append(f"{prefix}.operations must contain non-empty strings")
    elif kind == "gmail":
        if not isinstance(source.get("address"), str) or "@" not in source.get("address", ""):
            errors.append(f"{prefix}.address must be an email address")
        if source.get("provider", "gmail_mcp") not in ("gmail_mcp", "generic_mcp"):
            errors.append(f"{prefix}.provider must be gmail_mcp or generic_mcp")
        if not isinstance(source.get("server"), str) or not source["server"].strip():
            errors.append(f"{prefix}.server (MCP server name) is required")
        labels = source.get("labels", ["INBOX"])
        if not isinstance(labels, list) or any(not isinstance(label, str) or not label for label in labels):
            errors.append(f"{prefix}.labels must contain non-empty strings")
        push = source.get("push", {})
        if not isinstance(push, dict) or set(push) - _PUSH_KEYS:
            errors.append(f"{prefix}.push has unknown keys")
        elif push.get("enabled") is True and (not push.get("topic") or not push.get("credentials_path")):
            errors.append(f"{prefix}.push requires topic and credentials_path when enabled")
        if source.get("provider") == "generic_mcp" and not isinstance(source.get("mapping"), dict):
            errors.append(f"{prefix}.mapping is required for generic_mcp")
    elif kind == "calendar":
        if not isinstance(source.get("server"), str) or not source["server"].strip():
            errors.append(f"{prefix}.server (MCP server name) is required")
        if source.get("provider", "gcal_mcp") != "gcal_mcp":
            errors.append(f"{prefix}.provider must be gcal_mcp")
        for field in ("lookback_hours", "lookahead_days"):
            if field in source and not _non_negative_int(source[field]):
                errors.append(f"{prefix}.{field} must be a non-negative integer")
        if "initial_import" in source and not isinstance(source["initial_import"], bool):
            errors.append(f"{prefix}.initial_import must be boolean")
        if "account" in source and not isinstance(source["account"], str):
            errors.append(f"{prefix}.account must be a string")
    elif kind == "github":
        repos = source.get("repos", [])
        if not isinstance(repos, list) or any(not isinstance(repo, str) or "/" not in repo for repo in repos):
            errors.append(f"{prefix}.repos must contain owner/name strings")
        if source.get("provider", "gh") != "gh":
            errors.append(f"{prefix}.provider must be gh")
        if not repos and not source.get("route"):
            errors.append(f"{prefix} needs repos (polling through gh) or a route (host webhook) or both")
        events = source.get("events", [])
        if not isinstance(events, list) or any(not isinstance(e, str) or not e for e in events):
            errors.append(f"{prefix}.events must contain non-empty strings")
        for field in ("consume", "all_branches", "initial_import"):
            if field in source and not isinstance(source[field], bool):
                errors.append(f"{prefix}.{field} must be boolean")
        if "route" in source and not isinstance(source["route"], str):
            errors.append(f"{prefix}.route must be a string")


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
    if not isinstance(value.get("workspaces"), dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not v for k, v in (value.get("workspaces") or {}).items()
    ):
        errors.append("workspaces must map repo names to directory paths")

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
        for index, source in enumerate(sources):
            _validate_source(index, source, errors, seen_source_ids, value.get("allowed_operations") or [])

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
        "notifications": ("daily_limit", "batch_minutes", "reminder_cooldown_hours", "reminder_items", "unanswered_hours", "interrupt_cooldown_hours"),
        "limits": ("events_per_source_cycle", "checks_per_day", "llm_calls_per_day", "llm_input_tokens", "llm_output_tokens", "jobs_per_day", "max_bundles_per_tick", "signal_attempts"),
        "retention": ("observations_days", "results_days", "audit_days", "snippet_chars", "full_content_days", "signals_days"),
        "jobs": ("max_concurrent", "max_turns", "max_seconds", "max_auto_per_task"),
    }.items():
        nested = value.get(section)
        if not isinstance(nested, dict):
            continue
        for field in fields:
            if not _positive_int(nested.get(field)):
                errors.append(f"{section}.{field} must be a positive integer")

    observation = value.get("observation")
    if isinstance(observation, dict):
        for field in ("local_sessions", "historical_import", "session_auto_jobs"):
            if not isinstance(observation.get(field), bool):
                errors.append(f"observation.{field} must be boolean")
        for field in ("state_db_poll_seconds", "debounce_seconds", "session_quiet_minutes"):
            if not _non_negative_int(observation.get(field)):
                errors.append(f"observation.{field} must be a non-negative integer")
    context = value.get("context")
    if isinstance(context, dict):
        for field in ("memory", "sessions"):
            if not isinstance(context.get(field), bool):
                errors.append(f"context.{field} must be boolean")
        for field in ("timeout_seconds", "max_chars", "session_results"):
            if not _positive_int(context.get(field)):
                errors.append(f"context.{field} must be a positive integer")
        if not isinstance(context.get("memory_tool", ""), str):
            errors.append("context.memory_tool must be a string")
    retention = value.get("retention")
    if isinstance(retention, dict):
        kinds = retention.get("full_content_kinds")
        if not isinstance(kinds, list) or any(not isinstance(k, str) for k in kinds):
            errors.append("retention.full_content_kinds must be an array of strings")

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
        times = notifications.get("digest_times")
        if not isinstance(times, list) or any(not isinstance(t, str) or len(t) != 5 or t[2] != ":" or not t[:2].isdigit() or not t[3:].isdigit() for t in times):
            errors.append("notifications.digest_times must be an array of HH:MM strings (empty for a batch_minutes cadence)")
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

    if value.get("trust_level") not in (None, *TRUST_LEVELS):
        errors.append("trust_level must be cautious, balanced or autonomous")
    for flat, check in (("jobs_per_day", _positive_int), ("auto_jobs_per_task", _positive_int), ("auto_job_cooldown_minutes", _non_negative_int),
                        ("notices_per_day", _positive_int)):
        if value.get(flat) is not None and not check(value.get(flat)):
            errors.append(f"{flat} must be a {'non-negative' if check is _non_negative_int else 'positive'} integer")
    for flat in ("quiet_start", "quiet_end"):
        if value.get(flat) is not None and not isinstance(value.get(flat), (str, int)):
            errors.append(f"{flat} must be HH:MM")
    trust = value.get("trust")
    if isinstance(trust, dict):
        if trust.get("level") not in TRUST_LEVELS:
            errors.append("trust.level must be cautious, balanced or autonomous")
        overrides = trust.get("overrides")
        if not isinstance(overrides, dict) or any(
            not isinstance(rules, dict) or any(rule not in ("auto", "ask") for rule in rules.values()) for rules in overrides.values()
        ):
            errors.append("trust.overrides must map task kinds to {action_class: auto|ask}")
        auto_external = trust.get("auto_external")
        if not isinstance(auto_external, list) or any(not isinstance(op, str) for op in auto_external):
            errors.append("trust.auto_external must be an array of operation ids")
    ingress = value.get("ingress")
    if isinstance(ingress, dict):
        receiver = ingress.get("gmail_receiver")
        if not isinstance(receiver, dict):
            errors.append("ingress.gmail_receiver must be object")
        else:
            unknown_receiver = sorted(set(receiver) - _RECEIVER_KEYS)
            if unknown_receiver:
                errors.append("ingress.gmail_receiver has unknown keys: " + ", ".join(unknown_receiver))
            if not isinstance(receiver.get("enabled"), bool):
                errors.append("ingress.gmail_receiver.enabled must be boolean")
            if not _positive_int(receiver.get("port")) or receiver.get("port", 0) > 65535:
                errors.append("ingress.gmail_receiver.port must be a valid port")
            if receiver.get("enabled") is True and not isinstance(receiver.get("public_url"), str):
                errors.append("ingress.gmail_receiver.public_url must be a string")
            if not isinstance(receiver.get("require_oidc"), bool):
                errors.append("ingress.gmail_receiver.require_oidc must be boolean")
    jobs = value.get("jobs")
    if isinstance(jobs, dict):
        if not isinstance(jobs.get("enabled"), bool) or not isinstance(jobs.get("worktree"), bool):
            errors.append("jobs.enabled and jobs.worktree must be boolean")
        if jobs.get("model") not in (None, "") and not isinstance(jobs.get("model"), str):
            errors.append("jobs.model must be a string or null")
        for field in ("skills", "toolsets"):
            if not isinstance(jobs.get(field), list) or any(not isinstance(item, str) for item in jobs[field]):
                errors.append(f"jobs.{field} must be an array of strings")
        if not _non_negative_int(jobs.get("auto_cooldown_minutes")):
            errors.append("jobs.auto_cooldown_minutes must be a non-negative integer")
    desktop = value.get("desktop")
    if isinstance(desktop, dict) and not isinstance(desktop.get("panel_label"), str):
        errors.append("desktop.panel_label must be a string")
    return not errors, errors

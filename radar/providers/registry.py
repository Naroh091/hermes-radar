"""Tool-fingerprint registry: which adapter understands a given MCP server."""
from __future__ import annotations

from typing import Any, Callable

from .base import MailProvider
from .generic_mcp import GenericMcpMailProvider
from .mail_gmail_mcp import GmailMcpProvider

FINGERPRINTS: dict[str, dict[str, Any]] = {
    "gmail_mcp": {
        "kind": "gmail", "label": "Gmail MCP (gmail-mcp family)",
        "required": {"search_emails", "read_email", "get_thread"},
    },
    "gcal_mcp": {
        "kind": "calendar", "label": "Google Calendar MCP (@cocal/google-calendar-mcp)",
        "required": {"list-events"}, "any": {"search-events", "get-event", "list-calendars"},
    },
    "github_mcp": {
        "kind": "github", "label": "GitHub MCP",
        "required": {"list_pull_requests"}, "any": {"get_pull_request", "list_commits", "search_issues"},
    },
}


def classify_tools(tool_names: list[str] | set[str]) -> str | None:
    names = {str(name) for name in tool_names}
    for provider_id, spec in FINGERPRINTS.items():
        if spec["required"] <= names and (not spec.get("any") or spec["any"] & names):
            return provider_id
    return None


def kind_of(provider_id: str | None) -> str | None:
    spec = FINGERPRINTS.get(provider_id or "")
    return spec["kind"] if spec else None


def build_calendar_provider(call_mcp: Callable[..., Any], source: dict[str, Any], *, allow_write: bool = False, timezone: str = ""):
    from .calendar_gcal_mcp import GcalMcpProvider

    provider = str(source.get("provider") or "gcal_mcp")
    server = str(source.get("server") or "")
    if not server:
        raise ValueError(f"source {source.get('id')} has no MCP server")
    if provider == "gcal_mcp":
        return GcalMcpProvider(call_mcp, server, allow_write=allow_write, account=str(source.get("account") or ""), timezone=timezone)
    raise ValueError(f"unknown calendar provider {provider}")


def build_mail_provider(call_mcp: Callable[..., Any], source: dict[str, Any], *, allow_write: bool = False) -> MailProvider:
    provider = str(source.get("provider") or "gmail_mcp")
    server = str(source.get("server") or "")
    if not server:
        raise ValueError(f"source {source.get('id')} has no MCP server")
    if provider == "gmail_mcp":
        return GmailMcpProvider(call_mcp, server, allow_write=allow_write)
    if provider == "generic_mcp":
        return GenericMcpMailProvider(call_mcp, server, source.get("mapping") or {})
    raise ValueError(f"unknown mail provider {provider}")

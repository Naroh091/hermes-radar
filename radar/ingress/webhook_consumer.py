"""Consume host webhook events (``pre_gateway_dispatch``) that belong to radar routes.

The host turns every POST on the ``webhook`` platform into a gateway ``MessageEvent`` whose
``source.user_id`` is ``webhook:<route>`` and whose ``raw_message`` carries the provider payload. For
routes owned by radar the consumer records the event and returns ``{"action": "skip"}`` so no
agent turn runs; other routes are observed and left alone.
"""
from __future__ import annotations

import json
from typing import Any, Callable


def route_of(event: Any) -> str:
    source = getattr(event, "source", None)
    user_id = str(getattr(source, "user_id", "") or "")
    return user_id[len("webhook:"):] if user_id.startswith("webhook:") else ""


def payload_of(event: Any) -> dict[str, Any]:
    raw = getattr(event, "raw_message", None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def gmail_push_of(payload: dict[str, Any]) -> dict[str, str] | None:
    address = payload.get("emailAddress") or payload.get("account") or payload.get("email")
    if not isinstance(address, str) or not address.strip():
        return None
    return {
        "address": address.strip().lower(),
        "history_id": str(payload.get("historyId") or ""),
        "pubsub_id": str(payload.get("pubsubMessageId") or payload.get("messageId") or ""),
    }


class WebhookConsumer:
    """Routes: ``{route_name: {"kind": "gmail_push"|"github", "skip": bool}}``."""

    def __init__(self, routes: dict[str, dict[str, Any]], *, on_gmail_push: Callable[[dict[str, str]], None],
                 on_github: Callable[[str, dict[str, Any], str], None] | None = None):
        self.routes = dict(routes)
        self.on_gmail_push = on_gmail_push
        self.on_github = on_github

    def on_event(self, event: Any, **_: Any) -> dict[str, Any] | None:
        route = route_of(event)
        spec = self.routes.get(route)
        if not spec:
            return None
        payload = payload_of(event)
        kind = spec.get("kind")
        if kind == "gmail_push":
            push = gmail_push_of(payload)
            if push is None:
                return {"action": "skip", "reason": "radar: malformed gmail push"} if spec.get("skip", True) else None
            try:
                self.on_gmail_push(push)
            except Exception:  # the hook must never disturb unrelated traffic
                return None
            return {"action": "skip", "reason": "radar: gmail push consumed"} if spec.get("skip", True) else None
        if kind == "github" and self.on_github is not None:
            try:
                self.on_github(route, payload, str(getattr(event, "message_id", "") or ""))
            except Exception:
                return None
            return {"action": "skip", "reason": "radar: github event consumed"} if spec.get("skip", False) else None
        return None

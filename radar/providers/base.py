"""Provider interfaces. The pipeline talks to these, never to an MCP server by name."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ProviderError(RuntimeError):
    """The provider could not serve the request; callers retry later, never guess."""


@dataclass
class MailMessage:
    id: str
    thread_id: str = ""
    subject: str = ""
    sender: str = ""
    to: str = ""
    date: str = ""
    epoch: float | None = None
    snippet: str = ""
    body: str = ""
    labels: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "thread_id": self.thread_id, "subject": self.subject, "sender": self.sender,
            "to": self.to, "date": self.date, "epoch": self.epoch, "snippet": self.snippet, "body": self.body,
            "labels": list(self.labels), "attachments": list(self.attachments),
        }


class MailProvider:
    """Read-first mailbox access. Write verbs exist for F2 operations and are gated by the caller."""

    provider_id = "abstract"
    read_only = True

    def search(self, query: str, *, max_results: int = 20) -> list[MailMessage]:
        raise NotImplementedError

    def read(self, message_id: str) -> MailMessage:
        raise NotImplementedError

    def thread(self, thread_id: str, *, max_chars: int = 4000) -> str:
        raise NotImplementedError

    def draft(self, *, to: list[str], subject: str, body: str, thread_id: str | None = None,
              in_reply_to: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def reply(self, message_id: str, body: str, *, reply_all: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    def send(self, *, to: list[str], subject: str, body: str) -> dict[str, Any]:
        raise NotImplementedError

    def send_draft(self, draft_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def capabilities(self) -> dict[str, bool]:
        return {"search": True, "read": True, "thread": True, "draft": False, "reply": False, "send": False}


class CalendarProvider:
    provider_id = "abstract"

    def list_events(self, *, time_min: str, time_max: str, calendar_id: str = "primary") -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_event(self, event_id: str, *, calendar_id: str = "primary") -> dict[str, Any]:
        raise NotImplementedError


class RepoProvider:
    provider_id = "abstract"

    def events(self, repo: str, *, etag: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        raise NotImplementedError

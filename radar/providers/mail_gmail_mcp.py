"""Adapter for the gmail-mcp family (@artymclabin/gmail-mcp, GongRzhe/Gmail-MCP-Server)."""
from __future__ import annotations

from typing import Any, Callable

from . import mcp_parsing as parse
from .base import MailMessage, MailProvider, ProviderError

READ_TOOLS = frozenset({
    "search_emails", "read_email", "get_thread", "list_inbox_threads", "get_inbox_with_threads",
    "download_attachment", "download_email", "list_email_labels", "list_filters", "get_filter",
})
WRITE_TOOLS = frozenset({"draft_email", "reply_all", "send_email", "update_draft", "send_draft"})
MAX_BODY = 20000


class GmailMcpProvider(MailProvider):
    provider_id = "gmail_mcp"

    def __init__(self, call_mcp: Callable[..., Any], server: str, *, timeout: float = 30, allow_write: bool = False):
        if not server or not callable(call_mcp):
            raise ValueError("server and call_mcp are required")
        self.call_mcp = call_mcp
        self.server = server
        self.timeout = timeout
        self.read_only = not allow_write

    def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if tool not in READ_TOOLS and (self.read_only or tool not in WRITE_TOOLS):
            raise PermissionError(f"{tool} is not permitted on this provider")
        try:
            return self.call_mcp(self.server, tool, dict(arguments), timeout=self.timeout)
        except PermissionError:
            raise
        except Exception as exc:  # network, server, host envelope
            raise ProviderError(f"{self.server}/{tool}: {type(exc).__name__}: {str(exc)[:200]}") from exc

    def search(self, query: str, *, max_results: int = 20) -> list[MailMessage]:
        raw = self._call("search_emails", {"query": query, "maxResults": max(1, min(int(max_results), 100))})
        try:
            text = parse.content_texts(raw)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc
        return [
            MailMessage(id=item["id"], subject=item["subject"], sender=item["from"], date=item["date"], epoch=item["epoch"])
            for item in parse.parse_search_blocks(text)
        ]

    def read(self, message_id: str) -> MailMessage:
        raw = self._call("read_email", {"messageId": message_id})
        try:
            text = parse.content_texts(raw)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc
        detail = parse.parse_read_text(text)
        if not detail:
            raise ProviderError(f"unreadable message {message_id}")
        body = detail.get("body") or ""
        return MailMessage(
            id=message_id, thread_id=detail.get("thread_id") or "", subject=detail.get("subject") or "",
            sender=detail.get("sender") or "", to=detail.get("to") or "", date=detail.get("date") or "",
            epoch=parse.parse_date(detail.get("date") or ""), snippet=" ".join(body.split())[:500],
            body=body[:MAX_BODY], attachments=_attachments_from_text(text),
        )

    def thread(self, thread_id: str, *, max_chars: int = 4000) -> str:
        if not thread_id:
            return ""
        raw = self._call("get_thread", {"threadId": thread_id, "format": "minimal"})
        try:
            return parse.content_texts(raw)[:max_chars]
        except ValueError:
            return ""

    def draft(self, *, to: list[str], subject: str, body: str, thread_id: str | None = None,
              in_reply_to: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"to": list(to), "subject": subject, "body": body}
        if thread_id:
            args["threadId"] = thread_id
        if in_reply_to:
            args["inReplyTo"] = in_reply_to
        return {"result": parse.content_texts(self._call("draft_email", args))}

    def reply(self, message_id: str, body: str, *, reply_all: bool = False) -> dict[str, Any]:
        tool = "reply_all" if reply_all else "reply_all"
        return {"result": parse.content_texts(self._call(tool, {"messageId": message_id, "body": body}))}

    def send(self, *, to: list[str], subject: str, body: str) -> dict[str, Any]:
        return {"result": parse.content_texts(self._call("send_email", {"to": list(to), "subject": subject, "body": body}))}

    def send_draft(self, draft_id: str) -> dict[str, Any]:
        """Send a draft the owner already read (the exact text shown in the notice)."""
        return {"result": parse.content_texts(self._call("send_draft", {"draftId": draft_id}))}

    def capabilities(self) -> dict[str, bool]:
        write = not self.read_only
        return {"search": True, "read": True, "thread": True, "draft": write, "reply": write, "send": write}


def _attachments_from_text(text: str) -> list[str]:
    names: list[str] = []
    capture = False
    for line in (text or "").splitlines()[:200]:
        stripped = line.strip()
        if stripped.lower().startswith("attachments"):
            capture = True
            continue
        if capture:
            if not stripped:
                break
            names.append(stripped.lstrip("-• ").split(" (")[0][:120])
    return names[:20]

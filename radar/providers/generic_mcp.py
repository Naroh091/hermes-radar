"""Configurable adapter for mail MCP servers the registry does not recognise.

Config shape (under the source):
    provider: generic_mcp
    mapping:
      search: {tool: search_messages, args: {q: "{query}", limit: "{max_results}"}, parser: gmail_text}
      read:   {tool: get_message, args: {id: "{message_id}"}, parser: gmail_text}
      thread: {tool: get_thread, args: {id: "{thread_id}"}, parser: text}
Argument templates are formatted with the call parameters; ``parser`` selects how the result is read.
"""
from __future__ import annotations

from typing import Any, Callable

from . import mcp_parsing as parse
from .base import MailMessage, MailProvider, ProviderError

_PARSERS = ("gmail_text", "text", "json")


class GenericMcpMailProvider(MailProvider):
    provider_id = "generic_mcp"

    def __init__(self, call_mcp: Callable[..., Any], server: str, mapping: dict[str, Any], *, timeout: float = 30):
        if not isinstance(mapping, dict) or "search" not in mapping or "read" not in mapping:
            raise ValueError("generic_mcp mapping requires search and read operations")
        self.call_mcp = call_mcp
        self.server = server
        self.mapping = mapping
        self.timeout = timeout

    def _invoke(self, operation: str, **params: Any) -> tuple[Any, str]:
        spec = self.mapping.get(operation)
        if not isinstance(spec, dict) or not spec.get("tool"):
            raise ProviderError(f"operation {operation} is not mapped")
        args = {}
        for key, template in (spec.get("args") or {}).items():
            value = template
            if isinstance(template, str):
                try:
                    value = template.format(**params)
                except (KeyError, IndexError):
                    value = template
                if value.isdigit():
                    value = int(value)
            args[key] = value
        parser = spec.get("parser") or "gmail_text"
        if parser not in _PARSERS:
            raise ProviderError(f"unknown parser {parser}")
        try:
            raw = self.call_mcp(self.server, spec["tool"], args, timeout=self.timeout)
        except Exception as exc:
            raise ProviderError(f"{self.server}/{spec['tool']}: {str(exc)[:200]}") from exc
        return raw, parser

    def search(self, query: str, *, max_results: int = 20) -> list[MailMessage]:
        raw, parser = self._invoke("search", query=query, max_results=max_results)
        if parser == "json":
            inner = parse.unwrap(raw)
            items = inner if isinstance(inner, list) else (inner.get("messages") if isinstance(inner, dict) else []) or []
            return [
                MailMessage(id=str(item.get("id") or ""), subject=str(item.get("subject") or ""),
                            sender=str(item.get("from") or item.get("sender") or ""), date=str(item.get("date") or ""),
                            epoch=parse.parse_date(str(item.get("date") or "")))
                for item in items if isinstance(item, dict) and item.get("id")
            ]
        text = parse.content_texts(raw)
        return [
            MailMessage(id=item["id"], subject=item["subject"], sender=item["from"], date=item["date"], epoch=item["epoch"])
            for item in parse.parse_search_blocks(text)
        ]

    def read(self, message_id: str) -> MailMessage:
        raw, parser = self._invoke("read", message_id=message_id)
        if parser == "json":
            inner = parse.unwrap(raw)
            item = inner if isinstance(inner, dict) else {}
            body = str(item.get("body") or item.get("text") or "")
            return MailMessage(id=message_id, thread_id=str(item.get("threadId") or item.get("thread_id") or ""),
                               subject=str(item.get("subject") or ""), sender=str(item.get("from") or ""),
                               date=str(item.get("date") or ""), body=body[:20000], snippet=body[:500])
        detail = parse.parse_read_text(parse.content_texts(raw))
        if not detail:
            raise ProviderError(f"unreadable message {message_id}")
        body = detail.get("body") or ""
        return MailMessage(id=message_id, thread_id=detail.get("thread_id") or "", subject=detail.get("subject") or "",
                           sender=detail.get("sender") or "", date=detail.get("date") or "", body=body[:20000],
                           snippet=" ".join(body.split())[:500], epoch=parse.parse_date(detail.get("date") or ""))

    def thread(self, thread_id: str, *, max_chars: int = 4000) -> str:
        if not thread_id or "thread" not in self.mapping:
            return ""
        try:
            raw, _ = self._invoke("thread", thread_id=thread_id)
            return parse.content_texts(raw)[:max_chars]
        except (ProviderError, ValueError):
            return ""

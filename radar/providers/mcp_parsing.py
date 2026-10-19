"""Parsers for the text that real MCP mail servers return (ported from mail-intelligence).

The host wraps every MCP call as ``{"ok": bool, "result": ..., "truncated"?: True}`` and caps the raw
handler JSON at about 64 KB. These helpers unwrap that envelope, recover a payload cut mid-string and
parse the rendered ``search_emails`` / ``read_email`` output of the gmail-mcp family.
"""
from __future__ import annotations

import json
import re
from email.utils import parsedate_to_datetime
from typing import Any


def payload_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (str, bytes)):
        try:
            value = json.loads(payload)
        except ValueError:
            return {}
        if isinstance(value, dict):
            return value
    return {}


def unwrap(payload: Any) -> Any:
    """Unwrap the host MCP envelope. MCP errors raise instead of looking empty."""
    data = payload_dict(payload)
    if isinstance(data.get("ok"), bool) and not data["ok"]:
        raise ValueError(f"mcp error: {str(data.get('error'))[:200]}")
    inner = data.get("result", data)
    if "structuredContent" in data and not isinstance(inner, (dict, list)):
        inner = data["structuredContent"]
    return inner


_SEARCH_BLOCK = re.compile(
    r"^ID:\s*(?P<id>.+?)\s*\nSubject:\s*(?P<subject>.*?)\s*\nFrom:\s*(?P<from>.*?)\s*\nDate:\s*(?P<date>.*?)\s*(?:\n|$)",
    re.MULTILINE,
)
_READ_HEAD = re.compile(
    r"^Thread ID:\s*(?P<thread>.*?)\s*\nMessage-ID:.*?\nSubject:\s*(?P<subject>.*?)\s*\nFrom:\s*(?P<from>.*?)\s*\nTo:.*?\nDate:\s*(?P<date>.*?)\s*\n\n(?P<body>.*)$",
    re.MULTILINE | re.DOTALL,
)
_TRUNCATED_HANDLER = re.compile(r'^\s*\{\s*"result"\s*:\s*"')
_TRUNCATION_MARK = "… [truncated]"
_JSON_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}


def header_field(text: str, name: str) -> str:
    match = re.search(rf"(?im)^{re.escape(name)}:\s*(.*)$", text or "")
    return match.group(1).strip() if match else ""


def _unescape_json_string(text: str) -> str:
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            nxt = text[index + 1]
            if nxt in _JSON_ESCAPES:
                out.append(_JSON_ESCAPES[nxt])
                index += 2
                continue
            if nxt == "u" and index + 6 <= length:
                try:
                    out.append(chr(int(text[index + 2:index + 6], 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
        out.append(char)
        index += 1
    return "".join(out)


def recover_truncated_json_string(text: str) -> str:
    """Recover usable text from a host-truncated handler payload (``{"result": "Thread ID: ...``)."""
    match = _TRUNCATED_HANDLER.match(text)
    if not match:
        return text
    body = text[match.end():]
    if body.endswith(_TRUNCATION_MARK):
        body = body[: -len(_TRUNCATION_MARK)]
    if body.endswith('"}'):
        body = body[:-2]
    elif body.endswith('"'):
        body = body[:-1]
    return _unescape_json_string(body)


def content_texts(payload: Any) -> str:
    """Text carried by MCP content blocks (search/read/thread), or the recovered truncated string."""
    inner = unwrap(payload)
    if isinstance(inner, str):
        return recover_truncated_json_string(inner)
    if isinstance(inner, dict):
        texts = []
        for item in inner.get("content") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        if texts:
            return "\n".join(texts)
        if isinstance(inner.get("structuredContent"), str):
            return inner["structuredContent"]
        if isinstance(inner.get("text"), str):
            return inner["text"]
    if isinstance(inner, list):
        texts = [item.get("text") for item in inner if isinstance(item, dict) and isinstance(item.get("text"), str)]
        return "\n".join(texts)
    return ""


def parse_date(value: str) -> float | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    try:
        return parsed.timestamp()
    except (OverflowError, ValueError):
        return None


def parse_search_blocks(text: str) -> list[dict[str, Any]]:
    found = []
    for match in _SEARCH_BLOCK.finditer(text or ""):
        if match.group("id").strip():
            date = match.group("date").strip()
            found.append(
                {
                    "id": match.group("id").strip(),
                    "subject": match.group("subject").strip(),
                    "from": match.group("from").strip(),
                    "date": date,
                    "epoch": parse_date(date),
                }
            )
    return found


def parse_read_text(text: str) -> dict[str, Any]:
    match = _READ_HEAD.match(text or "")
    if match:
        return {
            "thread_id": match.group("thread").strip(),
            "subject": match.group("subject").strip(),
            "sender": match.group("from").strip(),
            "date": match.group("date").strip(),
            "to": header_field(text, "To"),
            "body": match.group("body").strip(),
        }
    text = text or ""
    if not text.strip():
        return {}
    return {
        "thread_id": header_field(text, "Thread ID"),
        "subject": header_field(text, "Subject"),
        "sender": header_field(text, "From"),
        "date": header_field(text, "Date"),
        "to": header_field(text, "To"),
        "body": text.strip(),
    }


def parse_thread_text(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text or "")
    except ValueError:
        return []
    if isinstance(data, dict):
        messages = data.get("messages")
        if isinstance(messages, list):
            return [m for m in messages if isinstance(m, dict)]
    return []


def message_id(item: dict[str, Any]) -> str:
    for key in ("id", "gmail_id", "message_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def automation_signals(sender: str, headers: dict[str, str] | None = None) -> list[str]:
    """Technical automation signals, never a reason by themselves to suppress."""
    signals: list[str] = []
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if headers.get("list-unsubscribe"):
        signals.append("list-unsubscribe")
    precedence = headers.get("precedence", "").lower()
    if precedence in ("bulk", "list", "junk"):
        signals.append("precedence:" + precedence)
    auto = headers.get("auto-submitted", "").lower()
    if auto and auto != "no":
        signals.append("auto-submitted:" + auto)
    if re.search(r"noreply|no-reply|donotreply|do-not-reply|mailer-daemon|notifications?@", sender or "", re.I):
        signals.append("noreply-sender")
    return signals

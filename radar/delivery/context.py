"""Email-thread context for notices: who wrote, when, and what they said. Pure formatting, no network."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any
from zoneinfo import ZoneInfo

from ..i18n import t

# Where a reply stops and the quoted history starts (bodies are whitespace-collapsed snippets).
_QUOTE_MARKERS = re.compile(
    r"(?:^|\s)(?:"
    r"On .{5,160}? wrote:|El .{5,160}? escribi[oó]:|Le .{5,160}? a écrit ?:|Am .{5,160}? schrieb.{0,80}?:"
    r"|-{2,} ?(?:Original Message|Mensaje original|Forwarded message|Mensaje reenviado) ?-{2,}"
    r"|From: .{3,200}? Sent: |De: .{3,200}? Enviado: "
    r")",
    re.I | re.S,
)
_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|rv|aw|wg|tr)\s*:\s*)+", re.I)
_MONTHS = {
    "es": ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}
MAX_OTHERS = 2  # other participants named in the header line


def strip_quoted(text: str) -> str:
    """Drop the quoted history that mail clients append below a reply."""
    body = " ".join(str(text or "").split())
    match = _QUOTE_MARKERS.search(body)
    if match and match.start() > 0:  # a body that is only a quote stays as it is
        return body[: match.start()].rstrip(" -–—")
    return body


def clean_subject(subject: str) -> str:
    return _SUBJECT_PREFIX.sub("", str(subject or "")).strip()


def display_name(sender: str, *, account: str = "", language: str = "es") -> str:
    """``Name <addr>`` → ``Name``; the owner's own address → “you” in the owner's language."""
    name, addr = parseaddr(str(sender or ""))
    if addr and account and addr.lower() == str(account).lower():
        return t(language, "you")
    return (name or addr or str(sender or "")).strip().strip('"')


def when(value: Any, *, language: str = "es", tz: str = "UTC", now_dt: datetime | None = None) -> str:
    """``19 oct 11:02`` in the owner's zone; the year is added only when it is not the current one."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(str(tz or "UTC"))
    except Exception:  # unknown zone name in config
        zone = timezone.utc
    local = moment.astimezone(zone)
    reference = (now_dt or datetime.now(timezone.utc)).astimezone(zone)
    months = _MONTHS.get(str(language or "es")[:2].lower(), _MONTHS["en"])
    text = f"{local.day} {months[local.month - 1]} {local:%H:%M}"
    return text if local.year == reference.year else f"{text} {local.year}"


def email_thread(signals: list[dict[str, Any]], *, language: str = "es", tz: str = "UTC",
                 now_dt: datetime | None = None, show_account: bool = False) -> dict[str, Any] | None:
    """Display-ready context of the latest email behind a task, or ``None`` when the task has no email."""
    seen: set[str] = set()
    emails: list[dict[str, Any]] = []
    for signal in signals:
        if signal.get("kind") != "email" or not signal.get("id") or signal["id"] in seen:
            continue
        seen.add(signal["id"])
        emails.append(signal)
    if not emails:
        return None
    emails.sort(key=lambda s: str(s.get("occurred_at") or ""), reverse=True)
    latest = emails[0]
    meta = latest.get("meta") or {}
    account = str(meta.get("account") or "")
    sender = display_name(meta.get("sender", ""), account=account, language=language)
    others: list[str] = []
    for signal in emails[1:]:
        name = display_name((signal.get("meta") or {}).get("sender", ""), account=account, language=language)
        if name and name != sender and name not in others:
            others.append(name)
    return {
        "sender": sender, "others": others[:MAX_OTHERS], "count": len(emails),
        "when": when(latest.get("occurred_at"), language=language, tz=tz, now_dt=now_dt),
        "subject": clean_subject(latest.get("title", "")), "excerpt": strip_quoted(latest.get("snippet") or ""),
        "account": account if show_account else "", "thread_id": str(meta.get("thread_id") or ""),
    }


def thread_context(store: Any, task_id: str, config: dict[str, Any], *, now_dt: datetime | None = None) -> dict[str, Any] | None:
    """Context for the task's notices from its linked email signals (latest message first)."""
    try:
        signals = store.signals_for_task(task_id, limit=30)
    except Exception:  # a missing task or a closed store must never block a notice
        return None
    owner = config.get("owner", {}) or {}
    mail_sources = [s for s in (config.get("sources") or []) if isinstance(s, dict) and s.get("kind") == "gmail"]
    return email_thread(signals, language=str(owner.get("language") or "es"), tz=str(owner.get("timezone") or "UTC"),
                        now_dt=now_dt, show_account=len(mail_sources) > 1)

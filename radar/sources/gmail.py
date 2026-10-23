"""Gmail through a mail provider: push-triggered or periodic sync by time cursor."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..pipeline.correlate import OPEN_STATUSES
from ..pipeline.triage import TriageDecision, triage_signal
from ..providers.base import MailMessage, MailProvider, ProviderError
from ..providers.mcp_parsing import automation_signals
from .base import SourceAdapter, SyncResult, fingerprint, iso

log = logging.getLogger(__name__)

RAW_HOLD_HOURS = 48
CURSOR_OVERLAP_SECONDS = 180
INBOX_AND_SENT = "{in:inbox in:sent}"  # Gmail's OR group


def address_of(sender: Any) -> str:
    text = str(sender or "").strip().lower()
    return text[text.rfind("<") + 1:].rstrip(">").strip() if "<" in text else text


class GmailSource(SourceAdapter):
    kind = "gmail"

    def __init__(self, source_row: dict[str, Any], config: dict[str, Any], store: Any, *, provider: MailProvider, preferences: list[dict[str, Any]] | None = None, **kwargs: Any):
        super().__init__(source_row, config, store, **kwargs)
        self.provider = provider
        self.preferences = preferences or []

    @property
    def address(self) -> str:
        return str(self.settings.get("address") or self.row.get("account_ref") or "")

    def _query(self, now_dt: datetime) -> str:
        """Inbox and sent mail: what the owner wrote in a tracked thread is the surest sign that it moved on
        (on 2026-10-23 Radar asked about a thread the owner had answered one minute earlier)."""
        labels = [str(l) for l in (self.settings.get("labels") or ["INBOX"])]
        scope = " ".join(f"label:{label}" if label.upper() != "INBOX" else INBOX_AND_SENT for label in labels) or INBOX_AND_SENT
        after = self.cursor.get("after_epoch")
        if isinstance(after, (int, float)) and after > 0:
            return f"{scope} after:{int(after) - CURSOR_OVERLAP_SECONDS}"
        hours = int(self.settings.get("initial_lookback_hours") or 24)
        return f"{scope} newer_than:{max(1, hours)}h"

    def _signal_from(self, message: MailMessage, now_dt: datetime, *, with_body: bool) -> dict[str, Any]:
        occurred = datetime.fromtimestamp(message.epoch, tz=timezone.utc) if message.epoch else now_dt
        subject = message.subject or "(no subject)"
        automation = automation_signals(message.sender)
        meta = {
            "account": self.address, "sender": message.sender, "to": message.to, "date": message.date,
            "thread_id": message.thread_id, "automation": automation, "attachments": list(message.attachments),
            "source_kind": "gmail", "thread_reply": subject.lower().startswith(("re:", "fwd:", "rv:")),
            "server": self.settings.get("server", ""),
        }
        if self.owner_sent(message):
            meta["owner_sent"] = True
        snippet_chars = int(self.config.get("retention", {}).get("snippet_chars", 500))
        body = message.body if with_body else ""
        return {
            "id": str(uuid4()), "source_id": self.id, "kind": "email", "occurred_at": iso(occurred),
            "external_ref": message.id, "group_key": f"thread:{self.address}:{message.thread_id}" if message.thread_id else f"loose:{self.address}:{fingerprint(message.sender, subject)[:16]}",
            "title": subject[:300], "snippet": " ".join((body or message.snippet or "").split())[:snippet_chars],
            "meta": meta, "fingerprint": fingerprint("email", self.address, message.id),
            "raw_content": body[:20000] if body else None,
            "raw_expires_at": iso(now_dt + timedelta(hours=RAW_HOLD_HOURS)) if body else None,
            "content_ref": f"gmail:{self.address}:{message.id}",
        }

    @staticmethod
    def _merge(full: MailMessage, listed: MailMessage) -> MailMessage:
        """The read message, with what only the search listing knew (date, epoch, sender) filled in."""
        full.epoch = full.epoch or listed.epoch
        full.date = full.date or listed.date
        full.subject = full.subject or listed.subject
        full.sender = full.sender or listed.sender
        full.thread_id = full.thread_id or listed.thread_id
        return full

    def owner_sent(self, message: MailMessage) -> bool:
        return bool(self.address) and address_of(message.sender) == self.address.strip().lower()

    def _tracked(self, group_key: str) -> bool:
        """True when an open task already follows this thread."""
        if not hasattr(self.store, "tasks_for_group"):
            return False
        try:
            return bool(self.store.tasks_for_group(group_key, statuses=OPEN_STATUSES, limit=1))
        except Exception:
            return False

    def ingest_message(self, message: MailMessage, now_dt: datetime, *, read_body: bool = True) -> str:
        """Returns ``added`` | ``ignored`` | ``duplicate``."""
        if self.store.signal_exists(self.id, message.id):
            return "duplicate"
        head = self._signal_from(message, now_dt, with_body=False)
        if head["meta"].get("owner_sent"):
            if not message.thread_id and read_body:
                # A search listing carries no thread id; only the thread tells whether a task is waiting on it.
                try:
                    message = self._merge(self.provider.read(message.id), message)
                    head = self._signal_from(message, now_dt, with_body=False)
                except ProviderError as exc:
                    log.warning("gmail read failed for %s: %s", message.id, exc)
            if not self._tracked(head["group_key"]):
                decision = TriageDecision(False, "owner_sent_untracked")  # the owner's own mail matters only where a task is waiting
            else:
                decision = triage_signal(head, self.preferences, self.config)
        else:
            decision = triage_signal(head, self.preferences, self.config)
        now = iso(now_dt)
        if not decision.keep:
            self.store.add_signal(
                signal_id=head["id"], source_id=self.id, kind="email", occurred_at=head["occurred_at"],
                external_ref=message.id, group_key=head["group_key"], title=head["title"], snippet=head["snippet"],
                meta=head["meta"], fingerprint=head["fingerprint"], now=now, triage_status="ignored",
                triage_reason=decision.reason, expires_at=self.signal_expiry(now_dt),
            )
            return "ignored"
        full = message
        if read_body and not message.body:
            try:
                full = self._merge(self.provider.read(message.id), message)
            except ProviderError as exc:
                log.warning("gmail read failed for %s: %s", message.id, exc)
                full = message
        signal = self._signal_from(full, now_dt, with_body=bool(full.body))
        signal["id"] = head["id"]
        added = self.store.add_signal(
            signal_id=signal["id"], source_id=self.id, kind="email", occurred_at=signal["occurred_at"],
            external_ref=message.id, group_key=signal["group_key"], title=signal["title"], snippet=signal["snippet"],
            meta=signal["meta"], fingerprint=signal["fingerprint"], now=now, raw_content=signal["raw_content"],
            raw_expires_at=signal["raw_expires_at"], content_ref=signal["content_ref"],
            expires_at=self.signal_expiry(now_dt),
        )
        return "added" if added else "duplicate"

    def sync(self, now_dt: datetime) -> SyncResult:
        result = SyncResult(cursor=dict(self.cursor))
        self.push_requested = False
        limit = int(self.config.get("limits", {}).get("events_per_source_cycle", 100))
        try:
            messages = self.provider.search(self._query(now_dt), max_results=limit)
        except (ProviderError, PermissionError) as exc:
            result.error = str(exc)[:300]
            result.cursor["last_sync_at"] = iso(now_dt)
            result.health = {"status": "error", "error": result.error, "at": iso(now_dt)}
            return result
        messages.sort(key=lambda m: m.epoch or 0)
        max_epoch = float(result.cursor.get("after_epoch") or 0)
        for message in messages:
            result.seen += 1
            try:
                outcome = self.ingest_message(message, now_dt)
            except Exception as exc:  # one bad message never stops the sweep
                log.warning("gmail ingest failed for %s: %s", message.id, exc)
                continue
            if outcome == "added":
                result.added += 1
            elif outcome == "ignored":
                result.ignored += 1
            if message.epoch:
                max_epoch = max(max_epoch, message.epoch)
        if max_epoch:
            result.cursor["after_epoch"] = max_epoch
        elif not result.cursor.get("after_epoch"):
            result.cursor["after_epoch"] = now_dt.timestamp()
        result.cursor["last_sync_at"] = iso(now_dt)
        result.health = {"status": "ok", "at": iso(now_dt), "seen": result.seen, "added": result.added}
        return result

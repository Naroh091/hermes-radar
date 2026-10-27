"""Before a summary slot, re-check the artifacts its items depend on, so settled work is news and not a stale ask.

Today this covers GitHub pull requests. A task that still asks the owner for a review or a comment on a PR is
looked up with ``gh`` shortly before the digest it would appear in. A merged or closed PR becomes a ``github``
signal bound to the task; the pipeline then settles the task (its open steps are withdrawn) and may send one
line of news instead. One API call per task, at most once every few hours, only when a slot is close.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..delivery.cadence import upcoming_slot
from ..delivery.dispatch import next_reminder_at
from ..models import TASK_TERMINAL, canonical_hash

log = logging.getLogger(__name__)

SOURCE_ID = "local:refresh"
LEAD_MINUTES = 20
MIN_INTERVAL = timedelta(hours=3)
MAX_PER_RUN = 5
PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
PR_NUMBER = re.compile(r"(?:\bPR|pull request|#)\s*#?(\d+)", re.I)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def pr_reference(task: dict[str, Any]) -> tuple[str, int] | None:
    """(repo, number) of the pull request a task is about, from its entities or its title."""
    entities = task.get("entities") or {}
    for key in ("pr", "url"):
        match = PR_URL.search(str(entities.get(key) or ""))
        if match:
            return match.group(1), int(match.group(2))
    repo = str(entities.get("repo") or "").strip()
    if repo.count("/") == 1:
        match = PR_NUMBER.search(str(task.get("title") or ""))
        if match:
            return repo, int(match.group(1))
    return None


def _ensure_source(store: Any, config: dict[str, Any], now: str) -> None:
    try:
        store.get_source(SOURCE_ID)
    except KeyError:
        store.add_source(source_id=SOURCE_ID, kind="hermes", account_ref="refresh", resource_scope={},
                         retention_days=int((config.get("retention") or {}).get("signals_days", 90)), now=now)


def refresh_before_digest(store: Any, config: dict[str, Any], now_dt: datetime, *, provider: Any,
                          lead_minutes: int = LEAD_MINUTES) -> dict[str, int]:
    """Look up the pull requests behind the digest items due at the slot (or paced reminder) within ``lead_minutes``.

    Returns how many tasks were checked and how many produced a signal. A provider failure never breaks the tick.
    """
    counts = {"checked": 0, "changed": 0}
    if provider is None:
        return counts
    slot = upcoming_slot(now_dt, config, lead_minutes)
    if slot is None:
        paced_at = next_reminder_at(store, config, now_dt)
        slot = max(paced_at, now_dt) if paced_at is not None and paced_at <= now_dt + timedelta(minutes=lead_minutes) else None
    if slot is None:
        return counts
    slot_iso, now = iso(slot), iso(now_dt)
    seen: set[str] = set()
    for proposal in store.list_proposals(statuses=("pending",), limit=200):
        if proposal.get("batch_key") != "digest":
            continue
        after = str((proposal.get("params") or {}).get("deliver_after") or "")
        task_id = str(proposal.get("task_id") or "")
        if (after and after > slot_iso) or not task_id or task_id in seen:
            continue
        seen.add(task_id)
        try:
            task = store.get_task(task_id)
        except KeyError:
            continue
        if task.get("status") in TASK_TERMINAL or task.get("muted"):
            continue
        reference = pr_reference(task)
        if reference is None:
            continue
        entities = dict(task.get("entities") or {})
        checked = _parse(entities.get("artifact_checked_at"))
        if checked is not None and checked + MIN_INTERVAL > now_dt:
            continue
        if counts["checked"] >= MAX_PER_RUN:
            break
        store.update_task(task_id, now=now, bump_version=False, entities={**entities, "artifact_checked_at": now})
        counts["checked"] += 1
        repo, number = reference
        try:
            pr = provider.pull_request(repo, number)
        except Exception as exc:  # a lookup that fails is just a lookup not made
            log.info("radar refresh: PR %s#%s not read: %s", repo, number, str(exc)[:160])
            continue
        if not isinstance(pr, dict) or str(pr.get("state") or "").lower() != "closed":
            continue
        merged = bool(pr.get("merged"))
        what = "merged" if merged else "closed without merging"
        when = _parse(pr.get("merged_at") or pr.get("closed_at")) or now_dt
        actor = str((pr.get("merged_by") or {}).get("login") or (pr.get("user") or {}).get("login") or "")
        title = f"PR #{number} {what}: {pr.get('title') or ''}".strip(": ")[:300]
        snippet = f"Pull request #{number} in {repo} was {what} on {iso(when)[:16].replace('T', ' ')} UTC" + (f" by {actor}" if actor else "") + "."
        _ensure_source(store, config, now)
        added = store.add_signal(
            signal_id=str(uuid4()), source_id=SOURCE_ID, kind="github", occurred_at=iso(when),
            external_ref=f"refresh:{repo}#{number}:{what}", group_key=f"github:{repo}:pr:{number}", title=title, snippet=snippet,
            meta={"source_kind": "github", "repo": repo, "event_type": "pull_request", "action": "closed", "number": number,
                  "actor": actor, "url": str(pr.get("html_url") or ""), "state": "closed", "merged": merged, "task_id": task_id, "refresh": True},
            fingerprint=canonical_hash({"refresh": repo, "pr": number, "what": what}), now=now,
            expires_at=iso(now_dt + timedelta(days=int((config.get("retention") or {}).get("signals_days", 90)))),
        )
        if added:
            counts["changed"] += 1
    return counts

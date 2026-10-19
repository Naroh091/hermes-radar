"""Outputs of the cron jobs radar created (automated patterns) come back as ``cron_result`` signals."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .base import SourceAdapter, SyncResult, fingerprint, iso

log = logging.getLogger(__name__)

SOURCE_ID = "local:cron"
_RESPONSE = re.compile(r"^## (?:Response|Output|Result)\s*$", re.M)
_SILENT = re.compile(r"^\s*\[SILENT\]\s*$")


def parse_output(text: str) -> dict[str, str]:
    name = ""
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first.startswith("# Cron Job:"):
        name = first.split(":", 1)[1].strip()
    run_time = ""
    match = re.search(r"^\*\*Run Time:\*\*\s*(.+)$", text, re.M)
    if match:
        run_time = match.group(1).strip()
    response = ""
    split = _RESPONSE.search(text)
    if split:
        response = text[split.end():].strip()
    return {"name": name, "run_time": run_time, "response": response}


class CronOutputSource(SourceAdapter):
    kind = "hermes"

    def __init__(self, source_row: dict[str, Any], config: dict[str, Any], store: Any, *, output_dir: Callable[[], Path] | Path, **kwargs: Any):
        super().__init__(source_row, config, store, **kwargs)
        self._output_dir = output_dir

    @property
    def interval_seconds(self) -> int:
        return 300

    def sync(self, now_dt: datetime) -> SyncResult:
        result = SyncResult(cursor=dict(self.cursor))
        self.push_requested = False
        seen: dict[str, str] = dict(result.cursor.get("files") or {})
        base = Path(self._output_dir() if callable(self._output_dir) else self._output_dir)
        for pattern in self.store.list_patterns(statuses=("automated",), limit=200):
            job_id = str(pattern.get("automation_ref") or "")
            if not job_id:
                continue
            folder = base / job_id
            if not folder.is_dir():
                continue
            files = sorted(p for p in folder.glob("*.md") if p.is_file())
            latest = seen.get(job_id, "")
            for path in files:
                if path.name <= latest:
                    continue
                result.seen += 1
                try:
                    parsed = parse_output(path.read_text(encoding="utf-8", errors="replace")[:60000])
                except OSError as exc:
                    log.warning("cron output unreadable %s: %s", path, exc)
                    continue
                seen[job_id] = path.name
                response = parsed["response"]
                if not response or _SILENT.match(response):
                    continue
                title = f"[cron] {parsed['name'] or pattern.get('name') or job_id}"
                meta = {"source_kind": "hermes", "job_id": job_id, "pattern_id": pattern["id"], "task_id": pattern.get("task_id"),
                        "run_time": parsed["run_time"], "file": path.name}
                added = self.store.add_signal(
                    signal_id=str(uuid4()), source_id=self.id, kind="cron_result", occurred_at=iso(now_dt), external_ref=f"cron:{job_id}:{path.name}",
                    group_key=f"task:{pattern.get('task_id')}" if pattern.get("task_id") else f"pattern:{pattern['id']}", title=title[:300],
                    snippet=" ".join(response.split())[:500], meta=meta, fingerprint=fingerprint("cron", job_id, path.name), now=iso(now_dt),
                    raw_content=response[:20000], expires_at=self.signal_expiry(now_dt),
                )
                if added:
                    result.added += 1
        result.cursor.update({"files": seen, "last_sync_at": iso(now_dt)})
        result.health = {"status": "ok", "at": iso(now_dt), "seen": result.seen, "added": result.added}
        return result

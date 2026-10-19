"""Candidate open tasks for a freshly extracted bundle (FTS + same-kind recency)."""
from __future__ import annotations

from typing import Any


def _entities_text(entities: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("places", "people", "organizations", "items"):
        parts.extend(str(v) for v in entities.get(key) or [])
    for item in entities.get("identifiers") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("value", "")))
    for item in entities.get("dates") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("when", "")))
    if entities.get("repo"):
        parts.append(str(entities["repo"]))
    return " ".join(parts)


def compact_task(task: dict[str, Any]) -> dict[str, Any]:
    checklist = task.get("checklist") or []
    missing = [c.get("item") for c in checklist if isinstance(c, dict) and c.get("status") == "missing"]
    return {
        "id": task.get("id"), "kind": task.get("kind"), "title": task.get("title"),
        "summary": (task.get("summary") or "")[:400], "status": task.get("status"), "due_at": task.get("due_at"),
        "entities": task.get("entities") or {}, "missing": missing[:8],
        "expectations": [e.get("description") for e in (task.get("expectations") or []) if isinstance(e, dict)][:6],
        "updated_at": task.get("updated_at"),
    }


def candidate_tasks(store: Any, extract: dict[str, Any], *, limit: int = 6) -> list[dict[str, Any]]:
    query = " ".join([extract.get("title", ""), extract.get("summary", ""), _entities_text(extract.get("entities") or {})])
    found: dict[str, dict[str, Any]] = {}
    for task in store.search_tasks(query, limit=limit):
        found[task["id"]] = task
    category = extract.get("category")
    if category and category != "noise":
        for task in store.list_tasks(statuses=("open", "waiting", "needs_user", "in_progress", "snoozed"), limit=4, kinds=(category,)):
            found.setdefault(task["id"], task)
    return [compact_task(task) for task in list(found.values())[:limit]]

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


def _identifier_text(entities: dict[str, Any]) -> str:
    parts: list[str] = [str(i.get("value", "")) for i in entities.get("identifiers") or [] if isinstance(i, dict)]
    for key in ("repo", "url"):
        if entities.get(key):
            parts.append(str(entities[key]))
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


OPEN_STATUSES = ("open", "waiting", "needs_user", "in_progress", "snoozed")


def candidate_tasks(store: Any, extract: dict[str, Any], *, limit: int = 6, session_id: str = "", group_key: str = "") -> list[dict[str, Any]]:
    """Open tasks the bundle may refer to. A task already fed by the same thread (``group_key``) comes
    first: a second advisory in the same mail thread is the same task, whatever its wording. A conversation
    bundle starts with the tasks born from the same session and never gets the same-kind fallback: sharing a
    kind is not sharing a subject."""
    entities = extract.get("entities") or {}
    if session_id:
        # Words shared with an unrelated task mean nothing here; only identifiers, repos and links do.
        query = " ".join(_identifier_text(entities).split())
    else:
        query = " ".join([extract.get("title", ""), extract.get("summary", ""), _entities_text(entities)])
    found: dict[str, dict[str, Any]] = {}
    if group_key and not session_id and hasattr(store, "tasks_for_group"):
        for task in store.tasks_for_group(group_key, statuses=OPEN_STATUSES, limit=limit):
            found[task["id"]] = task
    if session_id:
        for task in store.tasks_for_session(session_id, statuses=OPEN_STATUSES, limit=limit):
            found[task["id"]] = task
    for task in store.search_tasks(query, limit=limit) if query else []:
        found.setdefault(task["id"], task)
    category = extract.get("category")
    if category and category != "noise" and not session_id:
        for task in store.list_tasks(statuses=OPEN_STATUSES, limit=4, kinds=(category,)):
            found.setdefault(task["id"], task)
    return [compact_task(task) for task in list(found.values())[:limit]]

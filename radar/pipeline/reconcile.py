"""One open step per family and task.

A re-plan describes the task's next steps afresh. Before 2026-10-23 each plan added its proposals next to the
previous ones, and only the exact title was recognised as a duplicate, so a task collected nine or eleven
near-identical steps ("Enviar a Arturo el borrador ya preparado" / "… el borrador preparado") that all reached
the owner. Here a candidate that says the same thing as an open step (same family: the same question, the same
external action, the same piece of work, compared by word stems) updates that step in place, so the number the
owner already saw keeps pointing at it; an open step the new plan does not restate is withdrawn; a step that is
already running is never duplicated.

A question is one family whatever its form: on 2026-10-24 a plan carried the same doubt as a check (from its
verifications) and as an ask (with the fact to record), and the owner would have received it twice in a row. The ask
wins, because its answer records the fact; a check never rewrites an ask in place.
"""
from __future__ import annotations

from typing import Any

from .checklists import word_stems

OPEN = ("pending", "sent", "snoozed")
IN_FLIGHT = ("waiting", "accepted", "executing")
MIN_OVERLAP = 0.5  # Jaccard on word stems: "publicar comentario revisión PR 198" ~ "enviar comentario revisión PR 198"

Family = tuple[str, str, frozenset[str]]


def stems(title: Any) -> frozenset[str]:
    return frozenset(word_stems(str(title or "")))


def family(item: dict[str, Any]) -> Family:
    """``(kind, exact_key, stems)``: kind is question (ask or check) | inform | external | work; the exact key is the
    fact an ask records (two questions about the same fact are the same question, whatever their wording)."""
    operation = str(item.get("operation_id") or item.get("operation") or "")
    action_class = str(item.get("action_class") or "read")
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    fact = item.get("fact") if isinstance(item.get("fact"), dict) else params.get("fact") if isinstance(params.get("fact"), dict) else {}
    title_stems = stems(item.get("title"))
    if operation == "ask":
        return ("question", str(fact.get("key") or ""), title_stems)
    if operation == "check":
        return ("question", "", title_stems)
    if operation == "inform":
        return ("inform", "", title_stems)
    if action_class == "external":
        return ("external", operation, title_stems)  # one external action of a kind per task: two replies are one decision
    return ("work", "", title_stems)


def overlap(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def same_family(a: Family, b: Family) -> bool:
    if a[0] != b[0]:
        return False
    if a[0] == "inform":
        return True  # one piece of news per task: the newest wording wins
    if a[0] == "external":
        return (bool(a[1]) and a[1] == b[1]) or overlap(a[2], b[2]) >= MIN_OVERLAP
    if a[1] and b[1]:
        return a[1] == b[1]  # two questions about the same fact are one question; about different facts, two
    return overlap(a[2], b[2]) >= MIN_OVERLAP


def _score(a: Family, b: Family) -> float:
    if a[1] and a[1] == b[1]:
        return 1.0
    return overlap(a[2], b[2])


def _operation(item: dict[str, Any]) -> str:
    return str(item.get("operation_id") or item.get("operation") or "")


def _is_ask(item: dict[str, Any]) -> bool:
    return _operation(item) == "ask"


def collapse(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One candidate per family within a plan, in the plan's order; between two forms of the same question the ask
    stays, in the place of the first."""
    kept: list[dict[str, Any]] = []
    for item in candidates:
        fam = family(item)
        index = next((i for i, other in enumerate(kept) if same_family(fam, family(other))), None)
        if index is None:
            kept.append(item)
        elif _is_ask(item) and not _is_ask(kept[index]):
            kept[index] = item
    return kept


def _scheduled_later(row: dict[str, Any], now: str) -> bool:
    when = str((row.get("params") or {}).get("when") or "")
    return bool(when) and when > now


def reconcile(store: Any, task_id: str, candidates: list[dict[str, Any]], now: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Match the plan's candidates against the task's open steps.

    Returns the candidates to keep (each annotated with ``existing_id``/``existing_status``/``existing_expires_at``
    when it restates an open step, which the caller updates in place instead of inserting) and the ids of the open
    steps the plan did not restate, which the caller withdraws. A candidate whose family is already in flight
    (waiting, accepted, executing) is dropped: that work is being done. Steps the planner scheduled for a later
    moment are never withdrawn implicitly.
    """
    candidates = collapse(candidates)
    rows = store.list_proposals(task_id=task_id, limit=200)
    open_rows = [r for r in rows if r.get("status") in OPEN]
    in_flight = [family(r) for r in rows if r.get("status") in IN_FLIGHT]
    taken: set[str] = set()
    kept: list[dict[str, Any]] = []
    for item in candidates:
        fam = family(item)
        if any(same_family(fam, other) for other in in_flight):
            continue
        best: dict[str, Any] | None = None
        best_score = 0.0
        for row in open_rows:
            if row["id"] in taken:
                continue
            other = family(row)
            if not same_family(fam, other):
                continue
            score = _score(fam, other)
            if score > best_score:
                best, best_score = row, score
        if best is not None and _is_ask(best) and not _is_ask(item):
            taken.add(best["id"])  # the open ask already asks it, and records the answer: it stands as it is
            continue
        if best is not None and _is_ask(item) and not _is_ask(best):
            best = None  # an ask replaces the open check: the check is withdrawn below, the ask is inserted
        entry = dict(item)
        if best is not None:
            taken.add(best["id"])
            entry.update({"existing_id": best["id"], "existing_status": best.get("status"), "existing_expires_at": best.get("expires_at")})
        kept.append(entry)
    superseded = [r["id"] for r in open_rows if r["id"] not in taken and not _scheduled_later(r, now)]
    return kept, superseded


def dedupe_open(store: Any, task_id: str, now: str) -> int:
    """Keep one open step per family (the newest; of a question, the newest ask) and withdraw the rest. Returns how
    many were withdrawn."""
    rows = [r for r in store.list_proposals(task_id=task_id, limit=200) if r.get("status") in OPEN]
    rows.sort(key=lambda r: (_is_ask(r), str(r.get("created_at") or ""), str(r.get("id") or "")), reverse=True)
    keepers: list[Family] = []
    withdraw: list[str] = []
    for row in rows:
        fam = family(row)
        if any(same_family(fam, other) for other in keepers):
            withdraw.append(row["id"])
        else:
            keepers.append(fam)
    return store.supersede_proposals(withdraw, now=now, by="dedupe") if withdraw else 0

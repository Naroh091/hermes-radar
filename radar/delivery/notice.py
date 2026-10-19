"""Notice text and inline keyboards. Pure formatting, no network."""
from __future__ import annotations

import html
from typing import Any

from ..i18n import t
from ..pipeline.checklists import label as checklist_label

MAX_MESSAGE = 3800
MAX_RATIONALE = 600
DRAFT_INLINE = 1200  # longer drafts also travel as an attached text file
MAX_LABEL = 64  # Telegram shows about this much on a phone; longer labels are cut by the client
MAX_EXCERPT = 320  # quoted text of the latest email in the thread
CALLBACK_PREFIX = "pa"
TASK_PREFIX = "pt"


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def fit(value: Any, limit: int) -> str:
    """Cut prose at a word boundary with an ellipsis; text that already fits is returned untouched."""
    text = str(value or "")
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def _icon(language: str, kind: str) -> str:
    return t(language, f"icon_{kind}") if kind in {"trip", "purchase", "work_item", "commitment", "check", "routine", "other"} else t(language, "icon_other")


def proposal_buttons(proposal: dict[str, Any], language: str) -> list[dict[str, str]]:
    options = [o for o in (proposal.get("options") or []) if isinstance(o, dict) and o.get("id") and o.get("label")]
    if not options:
        options = [{"id": "do", "label": t(language, "btn_do")}, {"id": "no", "label": t(language, "btn_no")}]
    if len(options) == 1:
        options.append({"id": "no", "label": t(language, "btn_no")})
    row = []
    for option in options[:3]:
        data = f"{CALLBACK_PREFIX}:{proposal['id']}:{option['id']}"[:64]
        row.append({"label": fit(str(option["label"]), MAX_LABEL), "data": data})
    return row


def task_buttons(task_id: str, language: str) -> list[dict[str, str]]:
    return [
        {"label": t(language, "btn_later"), "data": f"{TASK_PREFIX}:{task_id}:later"[:64]},
        {"label": t(language, "btn_mute"), "data": f"{TASK_PREFIX}:{task_id}:mute"[:64]},
    ]


def build_notice(task: dict[str, Any], proposals: list[dict[str, Any]], *, language: str, message: str = "",
                 findings: list[str] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """One notice per task: headline, situation, email context, pending checklist, numbered proposals and buttons.

    ``context`` is the display-ready thread summary from :mod:`radar.delivery.context` (latest sender,
    date, subject, excerpt). The model is asked to write to size; if the pieces still exceed Telegram's
    limit, the longest optional parts (rationales, draft, excerpt, situation) shrink at word boundaries
    before anything is cut.
    """
    budgets = [(MAX_RATIONALE, DRAFT_INLINE, 800, MAX_EXCERPT), (300, 800, 800, 200), (150, 400, 500, 120), (0, 0, 300, 0)]
    text, buttons = "", []
    for rationale_cap, draft_cap, situation_cap, excerpt_cap in budgets:
        text, buttons = _compose(task, proposals, language=language, message=message, findings=findings, context=context,
                                 rationale_cap=rationale_cap, draft_cap=draft_cap, situation_cap=situation_cap, excerpt_cap=excerpt_cap)
        if len(text) <= MAX_MESSAGE:
            break
    text = fit(text, MAX_MESSAGE)
    return {
        "text": text, "parse_mode": "HTML", "buttons": buttons, "task_id": task["id"],
        "proposal_ids": [p["id"] for p in proposals[:4]], "kind": "notice",
    }


def context_lines(context: dict[str, Any] | None, *, language: str, excerpt_cap: int = MAX_EXCERPT) -> list[str]:
    """Header line (sender · date · thread size), subject and a quoted excerpt of the latest email."""
    if not context or not (context.get("sender") or context.get("excerpt")):
        return []
    head = [_esc(context.get("sender") or "")]
    if context.get("when"):
        head.append(_esc(context["when"]))
    if int(context.get("count") or 0) > 1:
        head.append(_esc(t(language, "thread_count", n=int(context["count"]))))
    others = [str(o) for o in context.get("others") or [] if o]
    if others:
        head.append(_esc(t(language, "thread_with", names=", ".join(fit(o, 30) for o in others))))
    label = _esc(t(language, "thread"))
    if context.get("account"):
        label += f" ({_esc(context['account'])})"
    lines = [f"<b>{label}:</b> " + " · ".join(part for part in head if part)]
    if context.get("subject"):
        lines.append(f"<i>{_esc(t(language, 'subject'))}: {_esc(fit(context['subject'], 120))}</i>")
    if context.get("excerpt") and excerpt_cap:
        lines.append(f"<blockquote>{_esc(fit(context['excerpt'], excerpt_cap))}</blockquote>")
    return lines


def _compose(task: dict[str, Any], proposals: list[dict[str, Any]], *, language: str, message: str,
             findings: list[str] | None, rationale_cap: int, draft_cap: int, situation_cap: int,
             context: dict[str, Any] | None = None, excerpt_cap: int = MAX_EXCERPT) -> tuple[str, list[list[dict[str, str]]]]:
    kind = task.get("kind", "other")
    lines = [f"<b>{_icon(language, kind)} {_esc(task.get('title'))}</b>"]
    situation = message or next((p.get("params", {}).get("message") for p in proposals if p.get("params", {}).get("message")), "") or task.get("summary", "")
    if situation:
        lines.extend(["", _esc(fit(situation, situation_cap))])
    thread = context_lines(context, language=language, excerpt_cap=excerpt_cap)
    if thread:
        lines.extend([""] + thread)
    missing = [c.get("item") for c in task.get("checklist") or [] if isinstance(c, dict) and c.get("status") == "missing"]
    if missing:
        lines.extend(["", f"<b>{_esc(t(language, 'checklist'))}:</b> " + "; ".join(_esc(checklist_label(m, language)) for m in missing[:4])])
    if task.get("due_at"):
        lines.append(f"<b>{_esc(t(language, 'due'))}:</b> {_esc(str(task['due_at'])[:16].replace('T', ' '))}")
    if findings:
        lines.extend(["", f"<b>{_esc(t(language, 'findings'))}:</b>"] + [f"• {_esc(fit(f, 300))}" for f in findings[:5]])
    buttons: list[list[dict[str, str]]] = []
    news = [p for p in proposals if p.get("operation_id") == "inform"]
    proposals = [p for p in proposals if p.get("operation_id") != "inform"]
    for item in news[:1]:
        if not situation and item.get("params", {}).get("message"):
            lines.extend(["", _esc(fit(item["params"]["message"], situation_cap))])
        buttons.append(proposal_buttons(item, language))
    if proposals:
        lines.extend(["", f"<b>{_esc(t(language, 'proposals'))}:</b>"])
        for index, proposal in enumerate(proposals[:4], 1):
            rationale = proposal.get("reason_text") or proposal.get("params", {}).get("rationale") or ""
            text = f"{index}. {_esc(proposal.get('title'))}"
            if rationale and rationale_cap:
                text += f" — {_esc(fit(rationale, rationale_cap))}"
            lines.append(text)
            row = proposal_buttons(proposal, language)
            if len(proposals) > 1:
                row[0]["label"] = fit(f"{index}️⃣ {row[0]['label']}", MAX_LABEL)
            buttons.append(row)
    draft = next((p.get("params", {}).get("draft") for p in proposals if p.get("params", {}).get("draft")), "")
    if draft and draft_cap:
        lines.extend(["", f"<b>{_esc(t(language, 'draft'))}:</b>", f"<i>{_esc(fit(draft, draft_cap))}</i>"])
    buttons.append(task_buttons(task["id"], language))
    return "\n".join(lines), buttons


def build_digest(groups: list[tuple[dict[str, Any], list[dict[str, Any]]]], *, language: str,
                 contexts: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    lines = [f"<b>📋 {_esc(t(language, 'digest_title'))}</b>"]
    buttons: list[list[dict[str, str]]] = []
    proposal_ids: list[str] = []
    counter = 0
    for task, proposals in groups[:8]:
        lines.extend(["", f"<b>{_icon(language, task.get('kind', 'other'))} {_esc(task.get('title'))}</b>"])
        summary = task.get("summary") or ""
        if summary:
            lines.append(_esc(summary[:300]))
        lines.extend(context_lines((contexts or {}).get(task["id"]), language=language, excerpt_cap=0))
        for proposal in proposals[:2]:
            counter += 1
            lines.append(f"{counter}. {_esc(proposal.get('title'))}")
            row = proposal_buttons(proposal, language)
            row[0]["label"] = fit(f"{counter}️⃣ {row[0]['label']}", MAX_LABEL)
            buttons.append(row)
            proposal_ids.append(proposal["id"])
    if counter == 0:
        lines.append(_esc(t(language, "digest_empty")))
    text = fit("\n".join(lines), MAX_MESSAGE)
    return {"text": text, "parse_mode": "HTML", "buttons": buttons, "proposal_ids": proposal_ids, "kind": "digest"}


def edited_text(original: str, outcome: str) -> str:
    base = original or ""
    return (fit(base, MAX_MESSAGE - len(outcome) - 4) + "\n\n" + f"<b>{_esc(outcome)}</b>") if outcome else base

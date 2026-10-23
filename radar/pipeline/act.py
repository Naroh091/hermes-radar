"""Apply a validated plan: tasks, links, facts, attention, proposals and review timers."""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..delivery.cadence import next_delivery
from ..i18n import t
from ..trust import may_auto_run
from .sessions import session_of, task_session_active
from ..models import TASK_TERMINAL, canonical_hash
from .attention import resolve
from .checklists import merge_checklist, template_for
from .schemas import TASK_KINDS

PROPOSAL_TTL_HOURS = 72
SCHEDULED_TTL_HOURS = 48
MIN_FACT_CONFIDENCE = 0.4
INFERRED_FACT_CAP = 0.6  # a model's reading of a message never outranks what the owner said
INTERRUPT_COOLDOWN_HOURS = 3


@dataclass
class ActResult:
    task_id: str | None
    task_status: str | None
    decision: str
    disposition: str
    attention_reason: str
    proposals: list[str] = field(default_factory=list)
    facts: int = 0
    message: str = ""
    created: bool = False


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _merge_entities(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(existing or {})
    for key, value in (new or {}).items():
        if isinstance(value, list):
            current = list(merged.get(key) or []) if isinstance(merged.get(key), list) else []
            seen = {canonical_hash(v) for v in current}
            for item in value:
                digest = canonical_hash(item)
                if digest not in seen:
                    current.append(item)
                    seen.add(digest)
            merged[key] = current[:40]
        elif value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _merge_expectations(existing: list[Any], new: list[Any]) -> list[Any]:
    merged: list[Any] = [e for e in existing if isinstance(e, dict)]
    seen = {str(e.get("description", "")).strip().lower() for e in merged}
    for item in new:
        if isinstance(item, dict):
            key = str(item.get("description", "")).strip().lower()
            if key and key not in seen:
                merged.append(item)
                seen.add(key)
    return merged[:15]


def _observation(signals: list[dict[str, Any]], task_kind: str) -> dict[str, Any]:
    meta = (signals[0].get("meta") or {}) if signals else {}
    kind = signals[0].get("kind", "") if signals else ""
    return {
        "account": meta.get("account", ""), "sender": meta.get("sender", ""), "message_type": kind,
        "project": meta.get("project", ""), "kind": task_kind, "source_kind": meta.get("source_kind", kind),
        "repo": meta.get("repo", ""),
    }



def _clip_title(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:") + "…"


DECLINE_MEMORY_HOURS = 24


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def declined_recently(store: Any, task_id: str, now_dt: datetime, hours: int = DECLINE_MEMORY_HOURS) -> list[dict[str, str]]:
    """Steps the owner refused for this task lately (not auto-expired ones, not whole-task snooze/mute)."""
    since = iso(now_dt - timedelta(hours=hours))
    out: list[dict[str, str]] = []
    for proposal in store.list_proposals(statuses=("declined",), task_id=task_id, limit=50):
        response = proposal.get("response") or {}
        if response.get("auto") or response.get("task_action"):
            continue
        if str(proposal.get("responded_at") or "") < since:
            continue
        out.append({
            "operation": str(proposal.get("operation_id") or ""), "action_class": str(proposal.get("action_class") or ""),
            "title": _norm(proposal.get("title")),
        })
    return out


def awaiting_owner(store: Any, task_id: str) -> bool:
    """True while a notice about this task sits unanswered in the owner's chat (or sleeps until they asked)."""
    return bool(store.list_proposals(statuses=("sent", "snoozed"), task_id=task_id, limit=1))


def interrupt_again(store: Any, config: dict[str, Any], task_id: str, urgency: str, now_dt: datetime) -> bool:
    """A task with an unanswered notice may interrupt once more only for something that cannot wait and only
    after the last message about it is older than the cooldown: five messages in two hours about one thread
    is noise, whatever each mail says."""
    if urgency != "now":
        return False
    hours = float((config.get("notifications") or {}).get("interrupt_cooldown_hours", INTERRUPT_COOLDOWN_HOURS))
    last = parse_iso(store.last_notice_at(task_id)) if hasattr(store, "last_notice_at") else None
    return last is None or now_dt - last >= timedelta(hours=hours)


def matches_declined(item: dict[str, Any], declined: list[dict[str, str]]) -> bool:
    """Same title, or the same operation with the same effect class, counts as the step the owner refused."""
    operation = str(item.get("operation") or "")
    action_class = str(item.get("action_class") or "read")
    title = _norm(item.get("title"))
    for entry in declined:
        if title and entry["title"] == title:
            return True
        if operation and entry["operation"] == operation and entry["action_class"] == action_class:
            return True
    return False


MAX_DRAFT_CHARS = 6000


def remember_draft(store: Any, task: dict[str, Any], extract: dict[str, Any], now: str) -> dict[str, Any] | None:
    """Keep the latest drafted reply on the task (``entities.draft``) so every notice can show it and
    the send step uses exactly that text. Returns the draft in force, if any."""
    text = " ".join(str(extract.get("proposed_reply") or "").split("\r"))
    text = text.strip()
    entities = dict(task.get("entities") or {})
    if text and not _has_correspondent(store, task["id"]):
        text = ""  # nobody to reply to (a chat session, a calendar event): a "reply" makes no sense
    if text:
        refs = [str(r) for r in (extract.get("job") or {}).get("refs") or [] if isinstance(r, (str, int))]
        ref = next((r for r in refs if "draft" in r.lower()), "")
        entities["draft"] = {"text": text[:MAX_DRAFT_CHARS], "ref": ref, "at": now}
        try:
            store.update_task(task["id"], now=now, bump_version=False, entities=entities)
        except (KeyError, ValueError, RuntimeError):
            pass
        return entities["draft"]
    current = entities.get("draft")
    return current if isinstance(current, dict) and current.get("text") else None


def remember_branch(store: Any, task: dict[str, Any], extract: dict[str, Any], now: str) -> dict[str, Any] | None:
    """Keep the branch a job pushed (``entities.branch``) so a later step opens the draft PR natively."""
    job = extract.get("job") if isinstance(extract.get("job"), dict) else {}
    name = " ".join(str(job.get("branch") or "").split())
    entities = dict(task.get("entities") or {})
    if name and job.get("status") in (None, "succeeded"):
        entities["branch"] = {
            "name": name, "repo": str(job.get("repo") or entities.get("repo") or ""), "base": str(job.get("base") or ""), "at": now,
            "findings": [str(f) for f in (job.get("findings") or [])[:8]], "refs": [str(r) for r in (job.get("refs") or [])[:6]],
        }
        if job.get("repo") and not entities.get("repo"):
            entities["repo"] = str(job["repo"])
        if job.get("pr"):
            entities["pr"] = str(job["pr"])
        try:
            store.update_task(task["id"], now=now, bump_version=False, entities=entities)
        except (KeyError, ValueError, RuntimeError):
            pass
        return entities["branch"]
    current = entities.get("branch")
    return current if isinstance(current, dict) and current.get("name") else None


def remember_pr(store: Any, task_id: str, url: str, now: str) -> None:
    """The draft PR exists: planners stop proposing to open it."""
    if not url:
        return
    try:
        task = store.get_task(task_id)
        entities = dict(task.get("entities") or {})
        entities["pr"] = url
        store.update_task(task_id, now=now, bump_version=False, entities=entities)
    except (KeyError, ValueError, RuntimeError):
        pass


def owner_addresses(config: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for source in config.get("sources") or []:
        if isinstance(source, dict) and source.get("kind") == "gmail" and source.get("address"):
            out.add(str(source["address"]).strip().lower())
    return out


def owner_authored(signals: list[dict[str, Any]], config: dict[str, Any]) -> bool:
    """True when the newest email in the bundle was written by the owner: they already know its content."""
    emails = [s for s in signals if s.get("kind") == "email"]
    if not emails:
        return False
    sender = str((emails[-1].get("meta") or {}).get("sender") or "").lower()
    address = sender[sender.rfind("<") + 1:].rstrip(">").strip() if "<" in sender else sender.strip()
    return bool(address) and address in owner_addresses(config)


def _has_correspondent(store: Any, task_id: str) -> bool:
    """True when the task has someone to answer: an email thread or a GitHub issue/PR."""
    try:
        return any(s.get("kind") in ("email", "github") for s in store.signals_for_task(task_id, limit=20))
    except Exception:
        return True


def forget_draft(store: Any, task_id: str, now: str) -> None:
    """The drafted reply was sent: stop showing it."""
    try:
        task = store.get_task(task_id)
    except KeyError:
        return
    entities = dict(task.get("entities") or {})
    if entities.pop("draft", None) is not None:
        store.update_task(task_id, now=now, bump_version=False, entities=entities)


def apply(
    store: Any, config: dict[str, Any], *, signals: list[dict[str, Any]], extract: dict[str, Any] | None,
    plan: dict[str, Any], now_dt: datetime, preferences: list[dict[str, Any]],
    existing_task: dict[str, Any] | None = None, owner_followup: bool = False,
) -> ActResult:
    """``owner_followup`` marks a bundle that answers a step the owner approved: its proposals are
    delivered now and outside the daily notice cap, because the owner was promised a result."""
    now = iso(now_dt)
    language = config.get("owner", {}).get("language", "es")
    extract = extract or {}
    match = plan["task_match"]
    decision = match["decision"]
    planned = plan["task"]
    facts_written = 0
    # What a model reads into a mail or a chat is a hypothesis, never knowledge: it is stored as "inferred",
    # capped below any fact the owner stated, and the planner must ask before building on it.
    for fact in extract.get("facts_learned") or []:
        if fact.get("confidence", 0) >= MIN_FACT_CONFIDENCE:
            try:
                store.upsert_fact(
                    key=fact["key"], value=fact["value"], confidence=min(float(fact["confidence"]), INFERRED_FACT_CAP),
                    provenance={"signal_id": signals[0]["id"] if signals else None, "at": now, "origin": "inferred"}, now=now,
                )
                facts_written += 1
            except (ValueError, KeyError, TypeError):
                continue

    task = existing_task
    if task is None and decision in ("advances", "completes", "contradicts") and match.get("task_id"):
        try:
            task = store.get_task(match["task_id"])
        except KeyError:
            task = None
            decision = "new"
    asks = [c for c in plan.get("checks") or [] if c.get("ask_user")]
    if task is None and decision == "none" and not (plan.get("proposals") or asks):
        return ActResult(None, None, "none", "silent", "no_task", facts=facts_written, message=plan.get("message", ""))

    created = False
    if task is None:
        kind = planned.get("kind") if planned.get("kind") in TASK_KINDS else (extract.get("category") if extract.get("category") in TASK_KINDS else "other")
        if decision == "none":
            kind = "check" if asks else kind
            decision = "new"
        task_id = str(uuid4())
        title = planned.get("title") or extract.get("title") or (signals[0].get("title") if signals else "") or kind
        checklist = merge_checklist(template_for(kind), plan.get("checklist") or [])
        due_at = parse_iso(planned.get("due_at"))
        review_at = parse_iso(planned.get("next_review_at"))
        entities = dict(extract.get("entities") or {})
        if session_of(signals):
            entities["session_id"] = session_of(signals)
        store.create_task(
            task_id=task_id, kind=kind, title=title[:200], summary=planned.get("summary") or extract.get("summary") or "",
            entities=entities, expectations=extract.get("expectations") or [],
            checklist=checklist, now=now, next_review_at=iso(review_at) if review_at else None,
            due_at=iso(due_at) if due_at else None, confidence=planned.get("confidence", 0.5),
            origin_signal_id=signals[0]["id"] if signals else None,
        )
        task = store.get_task(task_id)
        created = True
        roles = ["origin"] + ["context"] * (len(signals) - 1)
    else:
        task_id = task["id"]
        fields: dict[str, Any] = {}
        if planned.get("title") and decision in ("advances", "new"):
            fields["title"] = planned["title"][:200]
        if planned.get("summary"):
            fields["summary"] = planned["summary"]
        fields["entities"] = _merge_entities(task.get("entities") or {}, extract.get("entities") or {})
        fields["expectations"] = _merge_expectations(task.get("expectations") or [], extract.get("expectations") or [])
        fields["checklist"] = merge_checklist(task.get("checklist") or template_for(task.get("kind", "other")), plan.get("checklist") or [])
        due_at = parse_iso(planned.get("due_at"))
        if due_at:
            fields["due_at"] = iso(due_at)
        review_at = parse_iso(planned.get("next_review_at"))
        fields["next_review_at"] = iso(review_at) if review_at else None
        try:
            fields["confidence"] = float(planned.get("confidence", task.get("confidence", 0.5)))
        except (TypeError, ValueError):
            pass
        task = store.update_task(task_id, now=now, **fields)
        role = {"advances": "advances", "completes": "completes", "contradicts": "contradicts"}.get(decision, "context")
        roles = [role] * len(signals)
    for signal, role in zip(signals, roles):
        try:
            store.link_task_signal(task_id, signal["id"], role, now)
        except Exception:
            pass

    draft = remember_draft(store, task, extract, now)
    remember_branch(store, task, extract, now)

    target_status = planned.get("status") or ("done" if decision == "completes" else task.get("status"))
    if decision == "completes" and target_status not in TASK_TERMINAL:
        target_status = "done"
    if target_status and target_status != task.get("status"):
        try:
            task = store.set_task_status(task_id, target_status, now=now, reason=match.get("why") or plan.get("notify", {}).get("reason"))
        except (ValueError, RuntimeError):
            pass

    attention = resolve(
        _observation(signals, task.get("kind", "other")), preferences,
        model_decision=plan["notify"]["disposition"], risk_flags=extract.get("risk_flags") or [],
        info_complete=True, muted=bool(task.get("muted")),
    )
    disposition = attention.disposition
    urgency = str(plan["notify"].get("urgency") or "today")
    if disposition == "notify_now" and attention.reason in ("model_evaluation", "protected_exception") and urgency != "now":
        disposition = "digest"  # only what cannot wait interrupts; a protected risk flag keeps it out of silence, not out of the summary
    if task_session_active(store, task, now_dt, config) and not owner_followup:
        disposition = "silent"  # the owner is already in that conversation
    escalation = str((plan.get("escalation") or {}).get("decision") or "ask")
    terminal = task.get("status") in TASK_TERMINAL
    conversation = bool(session_of(signals))
    if (conversation or owner_authored(signals, config)) and (terminal or escalation == "inform") and not owner_followup:
        disposition = "silent"  # the owner lived it or wrote it: nothing to report back
    read_steps = [p for p in plan.get("proposals") or [] if p.get("action_class") == "read" and p.get("operation") not in ("check", "inform")]
    if (escalation == "handle" and read_steps and not owner_followup and disposition != "silent"
            and may_auto_run(config.get("trust") or {}, "read", kind=task.get("kind"), operation_id=read_steps[0].get("operation") or None)):
        disposition = "silent"  # the assistant looks first; the result re-plans and decides inform or ask
    inform_only = escalation == "inform" and not (plan.get("proposals") or asks) and disposition != "silent" and bool(plan.get("message"))
    # A decision only the owner can take, with no step to offer: the message itself travels, answered in words.
    ask_only = (not terminal and not inform_only and not owner_followup and (escalation == "ask" or task.get("status") == "needs_user")
                and not (plan.get("proposals") or asks) and disposition != "silent" and bool(plan.get("message")))
    proposal_ids: list[str] = []
    if not terminal or inform_only:  # news about a task that just completed still reaches the owner
        batch_key = "now" if owner_followup else {"notify_now": "now", "digest": "digest", "silent": "silent"}[disposition]
        if batch_key == "now" and not owner_followup and awaiting_owner(store, task_id) and not interrupt_again(store, config, task_id, urgency, now_dt):
            batch_key = "digest"  # the owner already has an unanswered notice about this task: no second interruption
        slot = next_delivery(now_dt, "today" if urgency == "now" else urgency, config) if batch_key == "digest" else None
        candidates: list[dict[str, Any]] = []
        for check in asks:
            candidates.append({
                "title": check["description"], "rationale": "", "action_class": "read", "operation": "check",
                "when": "", "urgency": "high" if check.get("severity") == "high" else "normal",
                "options": [{"id": "ok", "label": t(language, "btn_ok")}, {"id": "wrong", "label": t(language, "btn_wrong")}],
            })
        candidates.extend(plan.get("proposals") or [])
        if inform_only or ask_only:
            headline = re.split(r"(?<=[.!?])\s", str(plan.get("message") or "").strip(), maxsplit=1)[0]
            important = str(plan["notify"].get("importance") or "normal") == "high"
            candidates.append({
                "title": _clip_title(headline, 80), "rationale": "", "action_class": "read", "operation": "inform", "when": "",
                "urgency": ("high" if important else "normal") if ask_only else "normal" if disposition == "notify_now" else "low",
                "options": [{"id": "ok", "label": t(language, "btn_ack")}], "ask": ask_only,
            })
        declined = declined_recently(store, task_id, now_dt)
        suppressed = 0
        attempted = 0
        for item in candidates:
            if attempted >= 4:
                break
            if matches_declined(item, declined):
                suppressed += 1
                continue
            attempted += 1
            when = parse_iso(item.get("when"))
            item_batch = batch_key
            if when is not None and when > now_dt:
                item_batch = "now"  # a step for a chosen moment is its own notice at that moment, never summary filler
            elif slot is not None and (when is None or when < slot):
                when = slot  # grouped news waits for its summary slot
            deliver_after = iso(when) if when and when > now_dt else ""
            ttl = timedelta(hours=SCHEDULED_TTL_HOURS) if deliver_after else timedelta(hours=PROPOSAL_TTL_HOURS)
            expires_at = iso((when if deliver_after else now_dt) + ttl)
            fingerprint = canonical_hash({"task": task_id, "title": " ".join(item["title"].lower().split())})
            options = item.get("options") or [{"id": "do", "label": t(language, "btn_do")}, {"id": "no", "label": t(language, "btn_no")}]
            result = store.create_task_proposal(
                proposal_id=str(uuid4()), task_id=task_id, title=item["title"], reason_text=item.get("rationale") or "",
                action_class=item.get("action_class", "read"), operation_id=item.get("operation") or None,
                params={"when": item.get("when", ""), "deliver_after": deliver_after, "rationale": item.get("rationale", ""),
                        "message": plan.get("message", ""), "urgency": urgency, "importance": str(plan["notify"].get("importance") or "normal"),
                        **({"owner_followup": True} if owner_followup else {}),
                        **({"ask": True} if item.get("ask") else {}),
                        **({"draft": draft["text"], "draft_ref": draft.get("ref", "")} if draft else {})},
                options=options, urgency=item.get("urgency", "normal"), expires_at=expires_at, fingerprint=fingerprint,
                now=now, batch_key=item_batch,
            )
            if result.get("created"):
                proposal_ids.append(result["id"])
        review_at = parse_iso(planned.get("next_review_at"))
        if review_at and review_at > now_dt:
            store.upsert_followup(
                followup_id=f"task:{task_id}:review", goal_id="tasks", goal_title="Task reviews", reason="next_step",
                subject_key=f"task:{task_id}", next_check_at=iso(review_at), expires_at=iso(review_at + timedelta(days=30)),
                assumptions={"kind": "task_review", "task_id": task_id}, now=now, task_id=task_id,
                timezone=config.get("owner", {}).get("timezone", "UTC"),
            )
    else:
        suppressed = 0
        store.cancel_task_followups(task_id, now)
    store.add_audit(
        audit_id=str(uuid4()), event_type="pipeline", subject_type="task", subject_id=task_id,
        correlation_id=signals[0]["id"] if signals else None, actor_kind="scheduler", decision_code=decision,
        metadata={"disposition": disposition, "attention": attention.reason, "urgency": urgency, "proposals": len(proposal_ids),
                  "suppressed": suppressed, "status": task.get("status"), "created": created}, now=now,
    )
    return ActResult(
        task_id=task_id, task_status=task.get("status"), decision=decision, disposition=disposition,
        attention_reason=attention.reason, proposals=proposal_ids, facts=facts_written,
        message=plan.get("message", ""), created=created,
    )

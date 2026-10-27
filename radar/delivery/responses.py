"""Apply the owner's answers (buttons, replies, tools) to proposals and tasks. Pure store logic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..i18n import t
from ..models import TASK_TERMINAL, canonical_hash
from .cadence import next_delivery, next_morning, paced
from .context import thread_context

OPEN_PROPOSAL = ("pending", "sent", "snoozed")
WAITING = "waiting"  # approved, not started yet: can still be cancelled, not re-answered
CANCELLABLE = OPEN_PROPOSAL + (WAITING,)
SNOOZE_HOURS = 24
DECLINE_OPTIONS = {"no", "never", "decline", "reject", "cancel"}
ACCEPT_OPTIONS = {"do", "yes", "ok", "accept", "approve", "send", "go"}
LATER_OPTIONS = {"later", "snooze"}
DONE_OPTIONS = {"done", "already_done"}
NEGATIVE_LABELS = ("no", "nope", "don't", "do not", "descart", "cancel", "mejor no")
LATER_LABELS = ("dejarlo", "más tarde", "mas tarde", "later", "leave it", "después", "despues", "pospon")


def option_intent(proposal: dict[str, Any], option: str) -> str:
    """What a pressed option means: accept | decline | later | done | review.

    The planner's contract is that the first option means "do it". Any other option only executes
    when it reads as approval; "review it first" and the like show the details and keep the proposal open.
    """
    option = (option or "").strip().lower()
    options = [o for o in proposal.get("options") or [] if isinstance(o, dict) and o.get("id")]
    ids = [str(o["id"]).strip().lower() for o in options]
    label = next((str(o.get("label") or "") for o in options if str(o["id"]).strip().lower() == option), "").strip().lower()
    if option in LATER_OPTIONS or label.startswith(LATER_LABELS):
        return "later"
    if option in DONE_OPTIONS:
        return "done"
    if option in DECLINE_OPTIONS or option.startswith(("no_", "not_")) or label.startswith(NEGATIVE_LABELS):
        return "decline"
    if option in ACCEPT_OPTIONS or (ids and option == ids[0]):
        return "accept"
    if option in ids:
        from .feedback import classify

        intent = classify(label).get("intent")
        return {"approve": "accept", "reject": "decline", "later": "later"}.get(str(intent), "review")
    return "review"


def review_detail(store: Any, config: dict[str, Any], proposal: dict[str, Any]) -> str:
    """Everything the owner may want to read before deciding: the full draft, or the step and the situation."""
    language = config.get("owner", {}).get("language", "es")
    params = proposal.get("params") or {}
    options = [o for o in proposal.get("options") or [] if isinstance(o, dict) and o.get("label")]
    label = str(options[0]["label"]) if options else t(language, "btn_do")
    draft = str(params.get("draft") or "")
    context = thread_context(store, proposal["task_id"], config) if proposal.get("task_id") else None
    prefix = ""
    if context and context.get("sender"):
        when = f" ({context['when']})" if context.get("when") else ""
        prefix = t(language, "review_context", sender=context["sender"], when=when, excerpt=_clip(context.get("excerpt") or "", 600)) + "\n\n"
    if draft:
        ref = str(params.get("draft_ref") or "")
        return prefix + t(language, "review_draft", text=draft, ref=f" ({ref})" if ref else "", label=label)
    summary = ""
    if proposal.get("task_id"):
        try:
            summary = str(store.get_task(proposal["task_id"]).get("summary") or "")
        except KeyError:
            summary = ""
    rationale = str(proposal.get("reason_text") or params.get("rationale") or "")
    return prefix + t(language, "review_details", title=proposal.get("title", ""), rationale=rationale, summary=summary, label=label)


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:") + "…"


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def snooze_until(now_dt: datetime, config: dict[str, Any]) -> datetime:
    """"Remind me tomorrow" means the start of the next waking day (paced) or the first summary slot of the next day."""
    if paced(config):
        return next_morning(now_dt, config)
    return next_delivery(now_dt, "whenever", config) or (now_dt + timedelta(hours=SNOOZE_HOURS))


def snoozed_text(language: str, now_dt: datetime, until: datetime, config: dict[str, Any], title: str) -> str:
    if not paced(config) and next_delivery(now_dt, "whenever", config) is None:
        return t(language, "snoozed", hours=SNOOZE_HOURS, title=title)
    local_now, local_until = now_dt.astimezone(until.tzinfo), until
    clock = local_until.strftime("%H:%M")
    days = (local_until.date() - local_now.date()).days
    if days <= 0:
        when = t(language, "when_today", time=clock)
    elif days == 1:
        when = t(language, "when_tomorrow", time=clock)
    else:
        when = t(language, "when_date", date=local_until.strftime("%d/%m"), time=clock)
    return t(language, "snoozed_at", when=when, title=title)


def respond_to_proposal(
    store: Any, config: dict[str, Any], *, proposal_id: str, option: str, actor: str, now_dt: datetime,
    channel: str = "telegram", note: str = "",
) -> dict[str, Any]:
    """Returns ``{"outcome": accepted|declined|snoozed|stale, "text": ..., "proposal": ..., "job": bool}``."""
    language = config.get("owner", {}).get("language", "es")
    now = iso(now_dt)
    try:
        proposal = store.get_proposal(proposal_id)
    except KeyError:
        return {"outcome": "stale", "text": t(language, "unknown_callback"), "proposal": None}
    title = proposal.get("title", "")
    option = (option or "").strip().lower()
    is_check = proposal.get("operation_id") == "check"
    is_info = proposal.get("operation_id") == "inform"
    if proposal.get("operation_id") == "ask" and proposal.get("status") in CANCELLABLE:
        answered = _answer_ask(store, proposal, option=option, actor=actor, channel=channel, note=note, now_dt=now_dt, language=language)
        if answered is not None:
            return answered
    intent = ("decline" if option == "ok" else "accept" if option == "wrong" else option_intent(proposal, option)) if is_check else option_intent(proposal, option)
    if is_info and proposal.get("status") in CANCELLABLE:
        store.respond_proposal(proposal_id, decision="declined", response={**{"option": option, "actor": actor, "channel": channel, "note": note[:500]}, "ack": True}, now=now)
        store.add_audit(
            audit_id=str(uuid4()), event_type="owner_response", subject_type="proposal", subject_id=proposal_id,
            correlation_id=proposal.get("task_id"), actor_kind="verified_owner", decision_code="acknowledged",
            metadata={"option": option, "channel": channel}, now=now,
        )
        return {"outcome": "acknowledged", "text": t(language, "acknowledged"), "proposal": store.get_proposal(proposal_id)}
    if proposal.get("status") == WAITING and intent != "decline":
        return {"outcome": "stale", "text": t(language, "already_waiting", title=title), "proposal": proposal}
    if proposal.get("status") not in CANCELLABLE:
        return {"outcome": "stale", "text": t(language, "unknown_callback"), "proposal": proposal}
    response = {"option": option, "actor": actor, "channel": channel, "note": note[:500]}
    if intent == "review":
        store.add_audit(
            audit_id=str(uuid4()), event_type="owner_response", subject_type="proposal", subject_id=proposal_id,
            correlation_id=proposal.get("task_id"), actor_kind="verified_owner", decision_code="review",
            metadata={"option": option, "channel": channel}, now=now,
        )
        return {"outcome": "review", "text": t(language, "review_ack"), "detail": review_detail(store, config, proposal), "proposal": proposal}
    if intent == "decline":
        store.respond_proposal(proposal_id, decision="declined", response=response, now=now)
        outcome, text = "declined", t(language, "declined", title=title)
        if is_check and option == "ok":
            text = t(language, "check_ok", title=title)
            if proposal.get("task_id"):
                _close_check_task(store, proposal["task_id"], now)
        if proposal.get("operation_id") == "cron.create":
            from ..patterns import dismiss

            dismiss(store, proposal.get("task_id"), now)
    elif intent == "later":
        until_dt = snooze_until(now_dt, config)
        store.respond_proposal(proposal_id, decision="snoozed", response=response, now=now, snoozed_until=iso(until_dt))
        outcome, text = "snoozed", snoozed_text(language, now_dt, until_dt, config, title)
    elif intent == "done":
        store.respond_proposal(proposal_id, decision="declined", response={**response, "done": True}, now=now)
        outcome, text = "declined", t(language, "task_done", title=title)
        if proposal.get("task_id"):
            try:
                store.set_task_status(proposal["task_id"], "done", now=now, reason="owner_says_done")
                store.cancel_task_followups(proposal["task_id"], now)
            except (KeyError, ValueError, RuntimeError):
                pass
    else:
        store.respond_proposal(proposal_id, decision="accepted", response=response, now=now)
        outcome, text = "accepted", t(language, "accepted", title=title)
        if is_check:
            text = t(language, "check_wrong", title=title)
            if proposal.get("task_id"):
                try:
                    store.set_task_status(proposal["task_id"], "needs_user", now=now, reason="check_confirmed_wrong")
                except (KeyError, ValueError, RuntimeError):
                    pass
    store.add_audit(
        audit_id=str(uuid4()), event_type="owner_response", subject_type="proposal", subject_id=proposal_id,
        correlation_id=proposal.get("task_id"), actor_kind="verified_owner", decision_code=outcome,
        metadata={"option": option, "channel": channel}, now=now,
    )
    return {"outcome": outcome, "text": text, "proposal": store.get_proposal(proposal_id)}


def _answer_ask(store: Any, proposal: dict[str, Any], *, option: str, actor: str, channel: str, note: str,
                now_dt: datetime, language: str) -> dict[str, Any] | None:
    """The owner answered a question: the answer becomes a fact of theirs and a signal bound to the task, so the
    plan starts again from what they said. None when the option is not one of the question's answers."""
    options = [o for o in proposal.get("options") or [] if isinstance(o, dict) and o.get("id")]
    ids = [str(o["id"]).strip().lower() for o in options]
    if option not in ids:
        return None
    now = iso(now_dt)
    index = ids.index(option)
    label = str(options[index].get("label") or option)
    fact = (proposal.get("params") or {}).get("fact") or {}
    value = str(fact.get("yes" if index == 0 else "no") or label)
    key = str(fact.get("key") or "")
    if key:
        try:
            store.record_owner_fact(key=key, value=value, provenance={"proposal_id": proposal["id"], "at": now, "channel": channel}, now=now)
        except ValueError:
            key = ""
    store.respond_proposal(proposal["id"], decision="accepted", response={"option": option, "actor": actor, "channel": channel, "note": note[:500], "answer": label}, now=now)
    task_id = proposal.get("task_id")
    if task_id:
        try:
            source_id = store.ensure_owner_source(now)
            question = str(proposal.get("title") or "")
            store.add_signal(
                signal_id=str(uuid4()), source_id=source_id, kind="user_reply", occurred_at=now,
                external_ref=f"answer:{proposal['id']}", group_key=f"task:{task_id}", title=f"{question} → {label}",
                snippet=f"{question} {label}." + (f" {key} = {value}." if key else "") + (f" {note[:300]}" if note else ""),
                meta={"task_id": task_id, "proposal_id": proposal["id"], "question": question, "answer": label,
                      "fact": {"key": key, "value": value} if key else {}, "source_kind": "owner", "platform": channel},
                fingerprint=f"answer:{proposal['id']}", now=now,
            )
        except Exception:
            pass
    store.add_audit(
        audit_id=str(uuid4()), event_type="owner_response", subject_type="proposal", subject_id=proposal["id"],
        correlation_id=task_id, actor_kind="verified_owner", decision_code="answered",
        metadata={"option": option, "channel": channel, "fact": key}, now=now,
    )
    return {"outcome": "answered", "text": t(language, "ask_recorded", value=value), "proposal": store.get_proposal(proposal["id"])}


def _close_check_task(store: Any, task_id: str, now: str) -> None:
    try:
        task = store.get_task(task_id)
        if task.get("kind") == "check" and task.get("status") not in TASK_TERMINAL:
            store.set_task_status(task_id, "done", now=now, reason="check_confirmed_ok")
            store.cancel_task_followups(task_id, now)
    except (KeyError, ValueError, RuntimeError):
        pass


def learn_from_mute(store: Any, task: dict[str, Any], now: str) -> dict[str, Any] | None:
    """«No avises más» teaches something: the next mail of the same family (same sender, same subject stem) is
    silenced at triage instead of becoming a task again. Only mails qualify; a chat has no family."""
    from ..preferences import new_preference, subject_stem

    try:
        origin = store.get_signal(task.get("origin_signal_id") or "")
    except (KeyError, TypeError):
        return None
    meta = origin.get("meta") or {}
    sender = str(meta.get("sender") or "").strip().lower()
    stem = subject_stem(meta.get("subject") or origin.get("title") or "")
    if origin.get("kind") != "email" or not sender or not stem:
        return None
    pref_id = "mute:" + canonical_hash({"sender": sender, "stem": stem})[:16]
    data = new_preference(
        pref_id, instruction=f"Silenciado por el propietario desde «{task.get('title', '')[:80]}»: mensajes de {sender} sobre «{stem}».",
        scope={"sender": sender, "subject_stem": stem, "kind": "email"}, effect="silent", origin="explicit",
    )
    try:
        current = next((p for p in store.list_preferences() if p.get("id") == pref_id), None)
        store.save_preference(data, expected_version=int(current.get("version") or 0) if current else 0, now=now, feedback_ref=task.get("id"))
    except ValueError:
        return None
    return data


def act_on_task(store: Any, config: dict[str, Any], *, task_id: str, action: str, actor: str, now_dt: datetime) -> dict[str, Any]:
    language = config.get("owner", {}).get("language", "es")
    now = iso(now_dt)
    try:
        task = store.get_task(task_id)
    except KeyError:
        return {"outcome": "stale", "text": t(language, "unknown_callback")}
    title = task.get("title", "")
    action = (action or "").strip().lower()
    open_proposals = store.list_proposals(statuses=CANCELLABLE, task_id=task_id, limit=20)
    if action in {"later", "snooze"}:
        until_dt = snooze_until(now_dt, config)
        until = iso(until_dt)
        for proposal in [p for p in open_proposals if p.get("status") != WAITING]:
            store.respond_proposal(proposal["id"], decision="snoozed", response={"actor": actor, "task_action": action}, now=now, snoozed_until=until)
        if task.get("status") not in TASK_TERMINAL:
            store.update_task(task_id, now=now, bump_version=False, snoozed_until=until)
            try:
                store.set_task_status(task_id, "snoozed", now=now, reason="owner_snoozed")
            except (ValueError, RuntimeError):
                pass
        outcome, text = "snoozed", snoozed_text(language, now_dt, until_dt, config, title)
    elif action in {"mute", "never"}:
        for proposal in open_proposals:
            store.respond_proposal(proposal["id"], decision="declined", response={"actor": actor, "task_action": action}, now=now)
        store.update_task(task_id, now=now, bump_version=False, muted=True)
        store.cancel_task_followups(task_id, now)
        outcome, text = "muted", t(language, "muted", title=title)
        learned = learn_from_mute(store, task, now)
        if learned:
            text = t(language, "muted_with_preference", title=title, sender=learned.get("scope", {}).get("sender", ""))
    elif action == "done":
        for proposal in open_proposals:
            store.respond_proposal(proposal["id"], decision="declined", response={"actor": actor, "task_action": action}, now=now)
        if task.get("status") not in TASK_TERMINAL:
            try:
                store.set_task_status(task_id, "done", now=now, reason="owner_says_done")
            except (ValueError, RuntimeError):
                pass
        store.cancel_task_followups(task_id, now)
        outcome, text = "done", t(language, "task_done", title=title)
    else:
        return {"outcome": "stale", "text": t(language, "unknown_callback")}
    store.add_audit(
        audit_id=str(uuid4()), event_type="owner_response", subject_type="task", subject_id=task_id,
        correlation_id=None, actor_kind="verified_owner", decision_code=outcome, metadata={"action": action}, now=now,
    )
    return {"outcome": outcome, "text": text}


def resolve_reply(
    store: Any, config: dict[str, Any], *, outbox_row: dict[str, Any], intent: dict[str, Any], actor: str,
    now_dt: datetime, note: str = "",
) -> dict[str, Any]:
    """Map a free-text reply to a notice or a reminder onto its proposals.

    A number refers to the position in the message as it was shown, whatever has been answered since; a
    number that is already settled or does not exist says so instead of landing on another step. Without a
    number, a reminder with several open decisions asks which one; a notice about one task keeps its old
    manners (the first open step, or every step for a plain «no»)."""
    language = config.get("owner", {}).get("language", "es")
    payload = outbox_row.get("payload") or {}
    proposal_ids = [pid for pid in payload.get("proposal_ids") or [] if isinstance(pid, str)]
    open_ids = [pid for pid in proposal_ids if _is_open(store, pid)]
    kind = intent.get("intent")
    index = int(intent.get("proposal_index") or 0)
    reminder = outbox_row.get("kind") == "digest"
    target: str | None = None
    if index:
        if index > len(proposal_ids):
            return {"outcome": "unknown_number", "text": t(language, "reply_unknown_number", n=index, count=len(proposal_ids))}
        target = proposal_ids[index - 1]
        if not _is_open(store, target):
            return {"outcome": "already_answered", "text": t(language, "reply_already_answered", n=index, title=_title(store, target))}
    elif len(open_ids) == 1 or (open_ids and not reminder):
        target = open_ids[0]
    elif open_ids and kind in ("approve", "reject", "later", "done"):
        return {"outcome": "which_one", "text": t(language, "reply_which_one", count=len(proposal_ids))}
    task_id = outbox_row.get("task_id") or payload.get("task_id")
    if target and (index or not task_id):
        task_id = _task_of(store, target) or task_id
    if kind == "approve" and target:
        return respond_to_proposal(store, config, proposal_id=target, option=_answer(store, target, "yes"), actor=actor, now_dt=now_dt, note=note)
    if kind == "reject":
        targets = [target] if (target and (index or reminder)) else open_ids  # a bare «no» to a notice about one task declines its steps
        results = [respond_to_proposal(store, config, proposal_id=pid, option=_answer(store, pid, "no"), actor=actor, now_dt=now_dt, note=note) for pid in targets]
        return results[0] if results else {"outcome": "stale", "text": t(language, "unknown_callback")}
    if kind == "later" and task_id:
        return act_on_task(store, config, task_id=task_id, action="later", actor=actor, now_dt=now_dt)
    if kind == "done" and task_id:
        return act_on_task(store, config, task_id=task_id, action="done", actor=actor, now_dt=now_dt)
    if kind == "preference":
        return {"outcome": "preference", "text": t(language, "reply_ack_preference", text=note[:120])}
    if kind == "question":
        return {"outcome": "question", "text": t(language, "reply_ack_question")}
    if kind == "thanks":
        return {"outcome": "thanks", "text": ""}
    return {"outcome": "unclear", "text": t(language, "reply_ack_unclear")}


def _answer(store: Any, proposal_id: str, word: str) -> str:
    """A written yes/no answers a question as such: "yes" confirms a check and "no" reports the mistake."""
    try:
        proposal = store.get_proposal(proposal_id)
    except KeyError:
        proposal = {}
    operation = proposal.get("operation_id")
    if operation == "ask":  # a question's own two answers, first means yes
        ids = [str(o.get("id")) for o in proposal.get("options") or [] if isinstance(o, dict) and o.get("id")]
        if len(ids) >= 2:
            return ids[0] if word == "yes" else ids[1]
    if operation == "check":
        return "ok" if word == "yes" else "wrong"
    return "do" if word == "yes" else "no"


def _title(store: Any, proposal_id: str) -> str:
    try:
        return str(store.get_proposal(proposal_id).get("title") or "")
    except KeyError:
        return ""


def _task_of(store: Any, proposal_id: str) -> str | None:
    try:
        return store.get_proposal(proposal_id).get("task_id")
    except KeyError:
        return None


def _is_open(store: Any, proposal_id: str) -> bool:
    try:
        return store.get_proposal(proposal_id).get("status") in OPEN_PROPOSAL
    except KeyError:
        return False

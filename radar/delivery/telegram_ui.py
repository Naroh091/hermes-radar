"""Telegram surface: button callbacks, free-text replies to notices and the /radar command."""
from __future__ import annotations

import logging
from datetime import timezone
from typing import Any, Callable
from uuid import uuid4

from ..i18n import t
from ..jobs.service import start_or_wait
from ..trust import describe, level_summary
from .feedback import classify
from .dispatch import draft_document
from .notice import CALLBACK_PREFIX, DRAFT_INLINE, MAX_LABEL, TASK_PREFIX, edited_text, fit, proposal_buttons, task_buttons
from .responses import OPEN_PROPOSAL, act_on_task, resolve_reply, respond_to_proposal

log = logging.getLogger(__name__)


def authorize_update(config: dict[str, Any], update: Any) -> tuple[bool, str]:
    language = config.get("owner", {}).get("language", "es")
    destination = config.get("destination") or {}
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    if not config.get("enabled") or destination.get("platform") != "telegram":
        return False, t(language, "not_configured")
    if user is None or str(user.id) != str(destination.get("owner_platform_id")):
        return False, t(language, "actor_unauthorized")
    expected_chat = (destination.get("address") or {}).get("chat_id")
    if chat is None or str(chat.id) != str(expected_chat):
        return False, t(language, "wrong_destination")
    if getattr(chat, "type", "") != "private" and not destination.get("allow_groups"):
        return False, t(language, "groups_forbidden")
    return True, ""


def _after_accept(rt: Any, result: dict[str, Any], option: str) -> str:
    """Start the approved step and say honestly whether it is running. Returns text to append to the answer."""
    proposal = result.get("proposal") or {}
    if result.get("outcome") != "accepted" or not proposal or proposal.get("operation_id") in ("check", "inform"):
        return ""
    language = rt.config.get("owner", {}).get("language", "es")
    try:
        job, reason = start_or_wait(rt.store, rt.config, proposal=proposal, option=option, now_dt=rt.clock())
    except Exception as exc:
        log.warning("job not queued for proposal %s: %s", proposal.get("id"), exc)
        return t(language, "job_not_queued", reason=t(language, "block_error"))
    if job is not None:
        return t(language, "job_queued")
    return t(language, "job_not_queued", reason=t(language, f"block_{reason or 'unknown'}"))


def with_followup(result: dict[str, Any], suffix: str) -> dict[str, Any]:
    if suffix:
        result["text"] = f"{result.get('text', '')} {suffix}".strip()
    return result


def remaining_keyboard(rt: Any, outbox_row: dict[str, Any] | None) -> list[list[dict[str, str]]]:
    if not outbox_row:
        return []
    language = rt.config.get("owner", {}).get("language", "es")
    payload = outbox_row.get("payload") or {}
    rows: list[list[dict[str, str]]] = []
    task_id = outbox_row.get("task_id") or payload.get("task_id")
    open_count = 0
    for index, proposal_id in enumerate(payload.get("proposal_ids") or [], 1):
        try:
            proposal = rt.store.get_proposal(proposal_id)
        except KeyError:
            continue
        if proposal.get("status") not in OPEN_PROPOSAL:
            continue
        row = proposal_buttons(proposal, language)
        if len(payload.get("proposal_ids") or []) > 1:
            row[0]["label"] = fit(f"{index}️⃣ {row[0]['label']}", MAX_LABEL)
        rows.append(row)
        open_count += 1
    if task_id and open_count:
        try:
            task = rt.store.get_task(task_id)
            if task.get("status") not in {"done", "abandoned", "expired"} and not task.get("muted"):
                rows.extend(task_buttons(task_id, language))
        except KeyError:
            pass
    return rows


async def handle_callback(rt: Any, update: Any, context: Any = None) -> None:
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    data = str(getattr(query, "data", "") or "")
    ok, reason = authorize_update(rt.config, update)
    if not ok:
        await query.answer(reason[:200])
        return
    parts = data.split(":", 2)
    if len(parts) != 3:
        await query.answer()
        return
    prefix, ident, option = parts
    actor = f"telegram:{getattr(getattr(update, 'effective_user', None), 'id', '')}"
    now_dt = rt.clock()
    if prefix == CALLBACK_PREFIX:
        result = respond_to_proposal(rt.store, rt.config, proposal_id=ident, option=option, actor=actor, now_dt=now_dt)
        if result.get("outcome") == "review":
            try:
                await query.answer(str(result.get("text", ""))[:200])
            except Exception:
                pass
            await send_detail(rt, result)  # the notice and its buttons stay as they are
            return
        with_followup(result, _after_accept(rt, result, option))
    elif prefix == TASK_PREFIX:
        result = act_on_task(rt.store, rt.config, task_id=ident, action=option, actor=actor, now_dt=now_dt)
    else:
        await query.answer()
        return
    try:
        await query.answer(str(result.get("text", ""))[:200])
    except Exception:
        pass
    message = getattr(query, "message", None)
    if message is None or result.get("outcome") == "stale":
        return
    try:
        outbox_row = rt.store.outbox_by_platform_message(str(message.message_id))
        original = getattr(message, "text_html", None) or (outbox_row or {}).get("payload", {}).get("text") or getattr(message, "text", "")
        buttons = remaining_keyboard(rt, outbox_row)
        destination = {"address": {"chat_id": message.chat_id}}
        await rt.transports.edit("telegram", destination, str(message.message_id), {
            "text": edited_text(original, str(result.get("text", ""))), "parse_mode": "HTML", "buttons": buttons,
        })
    except Exception as exc:
        log.debug("notice edit skipped: %s", exc)


async def send_detail(rt: Any, result: dict[str, Any]) -> bool:
    """Send the full draft or details as its own message and record it, so a reply to it still resolves."""
    proposal = result.get("proposal") or {}
    text = str(result.get("detail") or "")
    if not text or not rt.transports.available("telegram"):
        return False
    language = rt.config.get("owner", {}).get("language", "es")
    now_dt = rt.clock()
    payload: dict[str, Any] = {"text": fit(text, 3900), "parse_mode": None, "kind": "detail", "urgent": True, "exempt": True,
                               "proposal_ids": [proposal.get("id")], "task_id": proposal.get("task_id")}
    draft = str((proposal.get("params") or {}).get("draft") or "")
    if len(text) > 3900 and draft:
        document = draft_document(str(proposal.get("task_id") or proposal.get("id") or "draft"), draft, language=language, now_dt=now_dt)
        if document:
            payload["document"] = document
    try:
        destination = rt.store.destination("configured:primary")
        receipt = await rt.transports.send("telegram", destination, payload)
    except Exception as exc:
        log.warning("detail message not sent: %s", exc)
        return False
    message_id = getattr(receipt, "platform_message_id", None)
    if message_id:
        now = iso_now(rt)
        rt.store.record_sent(
            outbox_id=str(uuid4()), destination_id="configured:primary", notification_key=f"detail:{proposal.get('id')}:{now}",
            payload=payload, platform_message_id=str(message_id), now=now, task_id=proposal.get("task_id"), kind="detail",
        )
    return True


async def handle_text_reply(rt: Any, event: Any) -> dict[str, Any] | None:
    """Free-text reply to a notice (``pre_gateway_dispatch``). Returns a skip directive when handled."""
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    platform = getattr(platform, "value", platform)
    if str(platform or "") != "telegram":
        return None
    reply_to = getattr(event, "reply_to_message_id", None)
    if not reply_to:
        return None
    destination = rt.config.get("destination") or {}
    if str(getattr(source, "user_id", "")) != str(destination.get("owner_platform_id")):
        return None
    if str(getattr(source, "chat_id", "")) != str((destination.get("address") or {}).get("chat_id")):
        return None
    row = rt.store.outbox_by_platform_message(str(reply_to))
    if not row or row.get("kind") not in {"notice", "digest", "detail"}:
        return None
    text = str(getattr(event, "text", "") or "")
    intent = classify(text)
    actor = f"telegram:{getattr(source, 'user_id', '')}"
    result = resolve_reply(rt.store, rt.config, outbox_row=row, intent=intent, actor=actor, now_dt=rt.clock(), note=text)
    if result.get("outcome") == "preference":
        from .learn import learn_preference

        result = await learn_preference(rt, text=text, task_id=row.get("task_id") or (row.get("payload") or {}).get("task_id"), actor=actor, now_dt=rt.clock())
    if result.get("outcome") == "accepted":
        with_followup(result, _after_accept(rt, result, "do"))
    if result.get("outcome") in {"question", "unclear"}:
        return None  # the agent answers with the pending proposals in its context and can resolve them
    ack = str(result.get("text") or "")
    if ack and rt.transports.available("telegram"):
        try:
            await rt.transports.send("telegram", rt.store.destination("configured:primary"), {"text": ack, "parse_mode": None})
        except Exception as exc:
            log.debug("reply ack not sent: %s", exc)
    return {"action": "skip", "reason": f"radar reply handled: {result.get('outcome')}"}


def session_identity(getenv: Callable[[str, str], str] | None = None) -> dict[str, str]:
    """Platform, chat, thread and user of the session running a slash command (host session env)."""
    if getenv is None:
        try:
            from gateway.session_context import get_session_env as getenv  # type: ignore[assignment]
        except ImportError:
            import os

            getenv = lambda name, default="": os.environ.get(name, default)  # noqa: E731
    return {
        "platform": getenv("HERMES_SESSION_PLATFORM", ""), "chat_id": getenv("HERMES_SESSION_CHAT_ID", ""),
        "chat_type": getenv("HERMES_SESSION_CHAT_TYPE", ""), "thread_id": getenv("HERMES_SESSION_THREAD_ID", ""),
        "user_id": getenv("HERMES_SESSION_USER_ID", ""),
    }


def verify_from_session(rt: Any, identity: dict[str, str]) -> str:
    """``/radar verify``: bind the configured destination to the chat and user issuing the command."""
    language = rt.config.get("owner", {}).get("language", "es")
    destination = rt.config.get("destination") or {}
    if not rt.config.get("enabled") or destination.get("platform") != "telegram":
        return t(language, "not_configured")
    if str(identity.get("platform") or "") != "telegram":
        return t(language, "not_configured")
    if str(identity.get("user_id") or "") != str(destination.get("owner_platform_id") or ""):
        return t(language, "actor_detail", user=identity.get("user_id") or "?", expected=destination.get("owner_platform_id") or "?")
    expected_chat = str((destination.get("address") or {}).get("chat_id") or "")
    if str(identity.get("chat_id") or "") != expected_chat:
        thread = identity.get("thread_id") or ""
        return t(language, "wrong_destination_detail", chat=identity.get("chat_id") or "?",
                 chat_type=identity.get("chat_type") or "?", thread=f", tema {thread}" if thread and language.startswith("es") else (f", topic {thread}" if thread else ""),
                 expected=expected_chat or "?")
    chat_type = str(identity.get("chat_type") or "private").lower()
    kind = "private" if chat_type in {"private", "dm", "direct", ""} else "group"
    if kind == "group" and not destination.get("allow_groups"):
        return t(language, "groups_forbidden")
    address = {"chat_id": expected_chat}
    thread_id = identity.get("thread_id") or (destination.get("address") or {}).get("thread_id")
    if thread_id:
        address["thread_id"] = str(thread_id)
    rt.store.verify_destination(
        destination_id="configured:primary", platform="telegram",
        gateway_connection_ref=destination.get("gateway_connection_ref") or "telegram:default",
        address=address, owner_platform_id=str(identity["user_id"]), kind=kind,
        capabilities={"text": True, "buttons": True, "documents": True}, now=iso_now(rt),
    )
    return t(language, "verified")


def iso_now(rt: Any) -> str:
    return rt.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def command_text(rt: Any, raw_args: str, *, set_trust: Callable[[str], None] | None = None,
                 identity: dict[str, str] | None = None) -> str:
    language = rt.config.get("owner", {}).get("language", "es")
    args = (raw_args or "").split()
    sub = args[0].lower() if args else "status"
    store = rt.store
    if sub == "verify":
        try:
            return verify_from_session(rt, identity if identity is not None else session_identity())
        except Exception as exc:
            log.exception("radar verification failed")
            return t(language, "verify_failed") + f" ({type(exc).__name__})"
    if sub == "status":
        open_tasks = store.list_tasks(statuses=("open", "waiting", "needs_user", "in_progress", "snoozed"), limit=200)
        pending = store.list_proposals(statuses=("pending", "sent", "snoozed"), limit=200)
        waiting = store.list_proposals(statuses=("waiting",), limit=200)
        head = t(language, "status_header", tasks=len(open_tasks), proposals=len(pending), waiting=len(waiting), signals=store.pending_signal_count())
        extra = f"\n{t(language, 'trust_set', level=rt.config.get('trust', {}).get('level', 'balanced'))}"
        return head + extra + ("\n" + t(language, "paused") if rt.paused else "")
    if sub == "list":
        tasks = store.list_tasks(statuses=("open", "waiting", "needs_user", "in_progress", "snoozed"), limit=15, include_muted=False)
        if not tasks:
            return t(language, "no_tasks")
        lines = []
        for task in tasks:
            pending = [p for p in store.list_proposals(statuses=("pending", "sent", "snoozed"), task_id=task["id"], limit=10)
                       if p.get("batch_key") != "silent"]  # silent suggestions are never sent, so they are not "pending" for the owner
            waiting = store.list_proposals(statuses=("waiting", "executing"), task_id=task["id"], limit=5)
            marks = (f" ({len(pending)} ⏳)" if pending else "") + (f" ({len(waiting)} ⏸)" if waiting else "")
            lines.append(f"• [{task['kind']}] {task['title']} — {task['status']}{marks}\n  id: {task['id'][:8]}")
        return "\n".join(lines)
    if sub == "prefs":
        from .learn import list_preferences_text

        return list_preferences_text(store, language)
    if sub == "patterns":
        from ..patterns import describe as describe_pattern

        rows = store.list_patterns(limit=20)
        if not rows:
            return t(language, "patterns_empty")
        return "\n".join([t(language, "patterns_header")] + [f"• {describe_pattern(p, language)}" for p in rows])
    if sub == "forget" and len(args) > 1:
        from .learn import forget_preference_text

        return forget_preference_text(store, args[1], language, now_dt=rt.clock())
    if sub == "pause":
        rt.paused = True
        return t(language, "paused")
    if sub == "resume":
        rt.paused = False
        return t(language, "resumed")
    if sub == "mute" and len(args) > 1:
        prefix = args[1]
        for task in store.list_tasks(limit=500):
            if task["id"].startswith(prefix):
                return act_on_task(store, rt.config, task_id=task["id"], action="mute", actor="command", now_dt=rt.clock()).get("text", "")
        return t(language, "unknown_callback")
    if sub == "trust" and len(args) > 1:
        level = args[1].lower()
        if level not in ("cautious", "balanced", "autonomous"):
            return t(language, "help")
        if set_trust is not None:
            set_trust(level)
        rt.config.setdefault("trust", {})["level"] = level
        return t(language, "trust_set", level=level) + "\n" + level_summary(level, language)
    return t(language, "help")

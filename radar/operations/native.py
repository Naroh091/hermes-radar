"""Native operations: well-defined actions the plugin performs itself, deterministically, with consent.

A proposal whose operation is native (``gmail.reply``, ``calendar.respond``, ``github.comment``) and
whose parameters can be resolved from the task does not need a delegated agent: it becomes a job of
kind ``native``. The owner's approval is recorded as a single-use HMAC consent bound to the exact
operation and parameters (``plan_hash``); the job consumes it right before acting, acts once through
the provider with write access, and verifies the effect by reading it back. Anything else stays on the
delegated ``hermes chat`` path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..consent import ConsentSigner
from ..models import canonical_hash
from ..reversibility import classify, ensure_origin_mark, validate_no_automerge

log = logging.getLogger("radar.native")

CONSENT_MINUTES = 30
NATIVE_KIND = "native"
ALIASES = {"gmail.reply_all": "gmail.reply", "gmail.send_reply": "gmail.reply", "github.comment_pr": "github.comment", "github.comment_issue": "github.comment"}
OPERATIONS: dict[str, dict[str, Any]] = {
    "gmail.reply": {"provider": "mail", "required": ("account", "message_id", "body"), "description": "Reply to the latest message of the thread with the approved text"},
    "calendar.respond": {"provider": "calendar", "required": ("event_id", "response"), "description": "Answer a calendar invitation (accepted, declined, tentative)"},
    "github.comment": {"provider": "repo", "required": ("repo", "number", "body"), "description": "Post the approved comment on the pull request or issue"},
    "github.open_pr": {"provider": "repo", "required": ("repo", "head", "title", "body"), "description": "Open a draft pull request for the branch a job pushed, with the origin mark"},
    "cron.create": {"provider": "cron", "required": ("schedule", "prompt", "name"), "description": "Schedule a recurring Hermes job that prepares or reports (a pattern the owner approved)"},
}
# Invitation answers are read from the option id or its label; a plain "no" declines the proposal, not the invitation.
_RESPONSE_STEMS = {"accepted": ("accept", "acept"), "declined": ("declin", "rechaz", "reject"), "tentative": ("tentativ", "maybe", "quiz")}
_DRAFT_ID = re.compile(r"\b(r-?\d{6,}|[A-Za-z0-9_-]{10,})\b")
_SENT_ID = re.compile(r"\b(?:message\s*id|id)\b\s*[:=]?\s*([0-9a-fA-F]{12,}|[A-Za-z0-9_-]{16,})", re.I)


@dataclass
class NativeOutcome:
    status: str  # succeeded | unknown | failed
    summary: str = ""
    effects: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    error: str | None = None


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve(operation_id: Any) -> str | None:
    key = str(operation_id or "")
    key = ALIASES.get(key, key)
    return key if key in OPERATIONS else None


def _address(sender: str) -> str:
    match = re.search(r"<([^>]+)>", str(sender or ""))
    return (match.group(1) if match else str(sender or "")).strip().lower()


def _draft_id(ref: Any) -> str:
    text = str(ref or "")
    match = re.search(r"draft\s*id[^A-Za-z0-9]{0,4}([A-Za-z0-9_-]{6,})", text, re.I)
    if match:
        return match.group(1)
    match = re.search(r"\b(r-?\d{8,})\b", text)
    return match.group(1) if match else ""


def plan(store: Any, proposal: dict[str, Any], task: dict[str, Any], option: str) -> dict[str, Any] | None:
    """``{"operation", "params"}`` when the proposal can run natively; None when it should be a delegated job."""
    operation = resolve(proposal.get("operation_id"))
    if operation is None:
        return None
    params: dict[str, Any] = dict((proposal.get("params") or {}).get("native") or {})
    try:
        signals = sorted(store.signals_for_task(task["id"], limit=30), key=lambda s: str(s.get("occurred_at") or ""), reverse=True)
    except Exception:
        signals = []
    draft = (proposal.get("params") or {}).get("draft") or ((task.get("entities") or {}).get("draft") or {}).get("text") or ""
    if operation == "gmail.reply":
        latest = next((s for s in signals if s.get("kind") == "email" and _address((s.get("meta") or {}).get("sender")) != str((s.get("meta") or {}).get("account") or "").lower()), None)
        if latest is None:
            return None
        meta = latest.get("meta") or {}
        params.setdefault("account", str(meta.get("account") or ""))
        params.setdefault("message_id", str(latest.get("external_ref") or ""))
        params.setdefault("thread_id", str(meta.get("thread_id") or ""))
        params.setdefault("to", _address(meta.get("sender")))
        params.setdefault("subject", str(latest.get("title") or ""))
        params.setdefault("body", str(draft or ""))
        params.setdefault("draft_id", _draft_id((proposal.get("params") or {}).get("draft_ref") or ((task.get("entities") or {}).get("draft") or {}).get("ref")))
    elif operation == "calendar.respond":
        latest = next((s for s in signals if s.get("kind") == "calendar"), None)
        if latest is None:
            return None
        meta = latest.get("meta") or {}
        params.setdefault("account", str(meta.get("account") or ""))
        params.setdefault("event_id", str(meta.get("event_id") or ""))
        params.setdefault("calendar_id", str(meta.get("calendar_id") or "primary"))
        params.setdefault("title", str(latest.get("title") or ""))
        label = next((str(o.get("label")) for o in proposal.get("options") or [] if isinstance(o, dict) and str(o.get("id")) == option), "")
        text = f"{option} {label}".lower()
        wanted = next((answer for answer, stems in _RESPONSE_STEMS.items() if any(stem in text for stem in stems)), "")
        if wanted:
            params.setdefault("response", wanted)
    elif operation == "github.comment":
        latest = next((s for s in signals if s.get("kind") == "github" and (s.get("meta") or {}).get("number")), None)
        if latest is None:
            return None
        meta = latest.get("meta") or {}
        params.setdefault("repo", str(meta.get("repo") or ""))
        params.setdefault("number", int(meta.get("number") or 0))
        params.setdefault("body", str(draft or ""))
        params.setdefault("url", str(meta.get("url") or ""))
    elif operation == "cron.create":
        entities = task.get("entities") or {}
        params.setdefault("schedule", str(entities.get("schedule_cron") or ""))
        params.setdefault("prompt", str(entities.get("prompt") or ""))
        params.setdefault("name", str(task.get("title") or "")[:80])
        try:
            destination = store.destination("configured:primary")
            address = destination.get("address") or {}
            origin = {"platform": str(destination.get("platform") or "telegram"), "chat_id": str(address.get("chat_id") or "")}
            if address.get("thread_id"):
                origin["thread_id"] = str(address["thread_id"])
            params.setdefault("origin", origin if origin["chat_id"] else None)
        except Exception:
            params.setdefault("origin", None)
    elif operation == "github.open_pr":
        entities = task.get("entities") or {}
        branch = entities.get("branch") if isinstance(entities.get("branch"), dict) else {}
        if entities.get("pr"):
            return None  # a draft PR already exists for this task: nothing native to do
        params.setdefault("repo", str(branch.get("repo") or entities.get("repo") or ""))
        params.setdefault("head", str(branch.get("name") or ""))
        params.setdefault("base", str(branch.get("base") or ""))
        params.setdefault("title", str((proposal.get("params") or {}).get("pr_title") or task.get("title") or "")[:120])
        findings = [str(f) for f in (branch.get("findings") or [])[:8]]
        body = str((proposal.get("params") or {}).get("pr_body") or task.get("summary") or "")
        if findings:
            body += "\n\n" + "\n".join(f"- {f}" for f in findings)
        language = str(branch.get("language") or "es")
        params.setdefault("body", ensure_origin_mark(body, task["id"], [r for r in (branch.get("refs") or [])[:6]], language))
        validate_no_automerge(operation, params)
    missing = [key for key in OPERATIONS[operation]["required"] if not params.get(key)]
    if missing:
        return None
    return {"operation": operation, "params": params}


def plan_hash(operation: str, params: dict[str, Any]) -> str:
    return canonical_hash({"operation": operation, "params": params})


def consent_key_path(hermes_home: Any) -> Path:
    return Path(str(hermes_home or Path.home() / ".hermes")) / "radar" / "secrets" / "consent.key"


def grant(store: Any, *, proposal: dict[str, Any], operation: str, params: dict[str, Any], actor_ref: str, channel: str,
          destination_id: str, hermes_home: Any, now_dt: datetime) -> dict[str, str]:
    """Record the owner's single-use consent for exactly this operation and these parameters."""
    signer = ConsentSigner.from_file(consent_key_path(hermes_home))
    expires_at = iso(now_dt + timedelta(minutes=CONSENT_MINUTES))
    digest = plan_hash(operation, params)
    token = signer.issue(proposal_id=proposal["id"], plan_hash=digest, actor_ref=actor_ref, destination_id=destination_id, expires_at=expires_at)
    consent_id = canonical_hash({"proposal": proposal["id"], "plan": digest, "at": iso(now_dt)})[:32]
    store.record_native_consent(
        consent_id=consent_id, proposal_id=proposal["id"], plan_hash=digest, actor_ref=actor_ref, verified_transport=channel or "telegram",
        destination_id=destination_id, expires_at=expires_at, token_hash=signer.token_hash(token), now=iso(now_dt),
    )
    return {"consent_id": consent_id, "plan_hash": digest, "actor_ref": actor_ref, "verified_transport": channel or "telegram", "destination_id": destination_id}


def job_prompt(operation: str, params: dict[str, Any], consent: dict[str, str]) -> str:
    return json.dumps({"operation": operation, "params": params, "consent": consent}, ensure_ascii=False, sort_keys=True)


def describe(operation: str, params: dict[str, Any]) -> str:
    if operation == "cron.create":
        return f"Schedule '{params.get('name')}' ({params.get('schedule')})"
    if operation == "github.open_pr":
        return f"Open draft PR {params.get('head')} on {params.get('repo')}"
    if operation == "gmail.reply":
        return f"Reply to {params.get('to')} ({params.get('subject', '')[:80]}) from {params.get('account')}"
    if operation == "calendar.respond":
        return f"Respond '{params.get('response')}' to {params.get('title', params.get('event_id'))}"
    if operation == "github.comment":
        return f"Comment on {params.get('repo')}#{params.get('number')}"
    return operation


# -- execution -------------------------------------------------------------------------------------
def _execute(operation: str, params: dict[str, Any], provider: Any) -> NativeOutcome:
    if operation == "gmail.reply":
        return _gmail_reply(params, provider)
    if operation == "calendar.respond":
        return _calendar_respond(params, provider)
    if operation == "github.comment":
        return _github_comment(params, provider)
    if operation == "github.open_pr":
        return _github_open_pr(params, provider)
    if operation == "cron.create":
        return _cron_create(params, provider)
    return NativeOutcome("failed", error=f"unknown native operation {operation}")


def _cron_create(params: dict[str, Any], provider: Any) -> NativeOutcome:
    created = provider.create(prompt=params["prompt"], schedule=params["schedule"], name=params["name"], origin=params.get("origin") or None)
    job_id = str(created.get("id") or "")
    effects = [f"Scheduled job '{params['name']}' ({params['schedule']})" + (f" id {job_id}" if job_id else "")]
    if not job_id:
        return NativeOutcome("failed", summary="The host did not return a job id", error="cron create returned no id")
    try:
        found = provider.get(job_id)
    except Exception as exc:
        return NativeOutcome("unknown", summary="Job created; reading it back failed", effects=effects, refs=[f"cron:{job_id}"], findings=[f"verification failed: {str(exc)[:160]}"])
    if found:
        return NativeOutcome("succeeded", summary=f"Scheduled '{params['name']}' as cron job {job_id} ({params['schedule']})", effects=effects, refs=[f"cron:{job_id}"])
    return NativeOutcome("unknown", summary="Job created but not found when reading back", effects=effects, refs=[f"cron:{job_id}"])


def _github_open_pr(params: dict[str, Any], provider: Any) -> NativeOutcome:
    repo, head = params["repo"], params["head"]
    existing = provider.pull_request_by_branch(repo, head) if hasattr(provider, "pull_request_by_branch") else None
    if existing and str(existing.get("state") or "").upper() != "CLOSED":
        return NativeOutcome("succeeded", summary=f"A pull request for {head} already exists: {existing.get('url')}", refs=[str(existing.get("url") or "")],
                             findings=[f"PR #{existing.get('number')} is {'a draft' if existing.get('isDraft') else 'ready for review'}"])
    base = params.get("base") or (provider.default_branch(repo) if hasattr(provider, "default_branch") else "main")
    created = provider.open_pr(repo, head=head, base=base, title=params["title"], body=params["body"], draft=True)
    url = str(created.get("url") or "")
    effects = [f"Draft pull request opened on {repo}: {head} → {base} ({url or 'no url returned'})"]
    try:
        check = provider.pull_request_by_branch(repo, head) if hasattr(provider, "pull_request_by_branch") else None
    except Exception as exc:
        return NativeOutcome("unknown", summary="PR created; reading it back failed", effects=effects, refs=[url] if url else [], findings=[f"verification failed: {str(exc)[:160]}"])
    if check and (not url or str(check.get("url")) == url):
        if not check.get("isDraft"):
            return NativeOutcome("unknown", summary="The pull request exists but GitHub does not report it as a draft", effects=effects, refs=[str(check.get("url"))])
        return NativeOutcome("succeeded", summary=f"Draft PR #{check.get('number')} opened and verified: {check.get('url')}", effects=effects, refs=[str(check.get("url"))])
    return NativeOutcome("unknown", summary="PR created but not found when reading back", effects=effects, refs=[url] if url else [])


def _gmail_reply(params: dict[str, Any], provider: Any) -> NativeOutcome:
    draft_id = str(params.get("draft_id") or "")
    if draft_id and hasattr(provider, "send_draft"):
        raw = provider.send_draft(draft_id)
        how = f"sent draft {draft_id}"
    else:
        raw = provider.reply(params["message_id"], params["body"], reply_all=bool(params.get("reply_all", True)))
        how = f"replied to message {params['message_id']}"
    text = str((raw or {}).get("result") or raw or "")
    match = _SENT_ID.search(text)
    sent_id = match.group(1) if match else ""
    effects = [f"Email sent to {params.get('to') or 'the thread'}: {params.get('subject', '')[:80]} ({how})"]
    refs = [f"gmail:{params.get('account')}:{sent_id}"] if sent_id else []
    if sent_id and hasattr(provider, "read"):
        try:
            message = provider.read(sent_id)
            return NativeOutcome("succeeded", summary=f"Sent and verified: {message.subject or params.get('subject', '')}", effects=effects, refs=refs,
                                 findings=[f"Sent message {sent_id} found in the mailbox (to {message.to or params.get('to')})"])
        except Exception as exc:
            return NativeOutcome("unknown", summary="The provider reported the send, but reading the sent message back failed", effects=effects, refs=refs,
                                 findings=[f"verification failed: {str(exc)[:160]}"])
    lowered = text.lower()
    if any(word in lowered for word in ("sent", "enviado", "success")):
        return NativeOutcome("unknown", summary="The provider reported the send; no message id came back to verify it", effects=effects, findings=[text[:200]])
    return NativeOutcome("failed", summary="The provider did not confirm the send", error=text[:300] or "empty provider answer")


def _calendar_respond(params: dict[str, Any], provider: Any) -> NativeOutcome:
    response = str(params.get("response") or "")
    provider.respond(params["event_id"], response, calendar_id=str(params.get("calendar_id") or "primary"), comment=str(params.get("comment") or ""))
    effects = [f"Invitation '{params.get('title') or params['event_id']}' answered: {response}"]
    try:
        event = provider.get_event(params["event_id"], calendar_id=str(params.get("calendar_id") or "primary"))
    except Exception as exc:
        return NativeOutcome("unknown", summary="Response sent; reading the event back failed", effects=effects, findings=[f"verification failed: {str(exc)[:160]}"])
    mine = str(event.get("my_response") or "").lower()
    if mine == response or (mine == "needsaction" and response == "tentative"):
        return NativeOutcome("succeeded", summary=f"Responded '{response}' and verified on the event", effects=effects, refs=[event.get("link") or params["event_id"]])
    return NativeOutcome("unknown", summary=f"Response sent but the event still shows '{mine or 'no response'}'", effects=effects, refs=[event.get("link") or params["event_id"]])


def _github_comment(params: dict[str, Any], provider: Any) -> NativeOutcome:
    created = provider.comment(params["repo"], int(params["number"]), params["body"])
    comment_id = created.get("id")
    effects = [f"Comment posted on {params['repo']}#{params['number']}"]
    refs = [created.get("url") or f"{params['repo']}#{params['number']}"]
    if comment_id and hasattr(provider, "get_comment"):
        try:
            found = provider.get_comment(params["repo"], int(comment_id))
            if str(found.get("id")) == str(comment_id):
                return NativeOutcome("succeeded", summary=f"Comment posted and verified: {created.get('url') or comment_id}", effects=effects, refs=refs)
        except Exception as exc:
            return NativeOutcome("unknown", summary="Comment posted; reading it back failed", effects=effects, refs=refs, findings=[f"verification failed: {str(exc)[:160]}"])
    return NativeOutcome("unknown" if comment_id else "failed", summary="Comment posted (unverified)" if comment_id else "GitHub returned no comment id", effects=effects if comment_id else [], refs=refs)


async def run_job(rt: Any, job: dict[str, Any]) -> Any:
    """Consume the consent, act once through a write-enabled provider, verify. Returns a JobOutcome."""
    from ..hostscope import profile_secret_scope
    from ..jobs.runner import JobOutcome

    try:
        spec = json.loads(job.get("prompt") or "{}")
        operation, params, consent = str(spec["operation"]), dict(spec["params"]), dict(spec.get("consent") or {})
    except (ValueError, KeyError, TypeError) as exc:
        return JobOutcome("failed", error=f"native job spec unreadable: {exc}")
    now = iso(rt.clock())
    digest = plan_hash(operation, params)
    if digest != consent.get("plan_hash"):
        return JobOutcome("failed", error="native job parameters do not match the consented plan")
    try:
        rt.store.consume_consent(consent["consent_id"], plan_hash=digest, actor_ref=consent["actor_ref"], destination_id=consent["destination_id"],
                                 verified_transport=consent["verified_transport"], now=now)
    except Exception as exc:
        return JobOutcome("failed", error=f"consent not usable: {str(exc)[:160]}")
    provider = rt.write_provider(OPERATIONS[operation]["provider"], account=str(params.get("account") or ""))
    if provider is None:
        return JobOutcome("failed", error=f"no write-enabled provider for {operation} ({params.get('account') or 'default'})")
    try:
        with profile_secret_scope(getattr(rt, "hermes_home", None)):
            outcome = await asyncio.to_thread(_execute, operation, params, provider)
    except PermissionError as exc:
        return JobOutcome("failed", error=f"refused: {exc}")
    except Exception as exc:
        # The provider may have acted before failing: never retry blindly, report as uncertain.
        return JobOutcome("unknown", summary=f"{describe(operation, params)}: the provider failed after dispatch", error=f"{type(exc).__name__}: {str(exc)[:200]}",
                          result={"effects": [], "findings": [f"{type(exc).__name__}: {str(exc)[:200]}"], "refs": []})
    status = outcome.status if outcome.status in {"succeeded", "unknown", "failed"} else "unknown"
    return JobOutcome(status, summary=outcome.summary or describe(operation, params), error=outcome.error,
                      result={"effects": outcome.effects, "findings": outcome.findings, "refs": outcome.refs, "operation": operation, "verified": status == "succeeded"})


def effect_class(operation: str) -> str:
    return classify(operation)

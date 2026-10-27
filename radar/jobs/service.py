"""Create jobs from accepted proposals and turn their outcomes back into signals."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..models import TASK_TERMINAL
from ..trust import EMAIL_SEND, may_auto_run
from ..operations import native
from .prompts import build_job_prompt
from .runner import JobOutcome

log = logging.getLogger(__name__)

JOBS_SOURCE_ID = "local:jobs"
NATIVE_DESTINATION = "configured:primary"
# Blocks that clear on their own (a running job ends, the cooldown passes, the daily budget and the per-task cap
# reset at midnight, the owner re-enables jobs): the step waits, it is never handed to the owner as a button.
DEFER_REASONS = frozenset({"active_job", "cooldown", "budget", "task_cap", "disabled"})
# Blocks that never clear for this proposal.
TERMINAL_BLOCKS = frozenset({"check", "no_task", "task_closed", "external_auto", "no_safe_executor"})


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def start_of_day(now_dt: datetime) -> str:
    return iso(now_dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))


def ensure_jobs_source(store: Any, now: str, retention_days: int) -> str:
    try:
        store.get_source(JOBS_SOURCE_ID)
    except KeyError:
        store.add_source(
            source_id=JOBS_SOURCE_ID, kind="hermes", account_ref="jobs", resource_scope={}, retention_days=retention_days, now=now,
        )
    return JOBS_SOURCE_ID


def workdir_for(config: dict[str, Any], task: dict[str, Any]) -> str | None:
    repo = str((task.get("entities") or {}).get("repo") or "").strip().lower()
    workspaces = config.get("workspaces") or {}
    if not repo:
        return None
    for key, path in workspaces.items():
        if str(key).strip().lower() in (repo, repo.split("/")[-1]):
            return str(path)
    return None


def job_block_reason(
    store: Any, config: dict[str, Any], *, proposal: dict[str, Any], now_dt: datetime, auto: bool = False,
    is_native: bool = False,
) -> str | None:
    """Why a job for ``proposal`` cannot start right now, or None.

    ``auto`` marks steps the plugin starts on its own: external actions never run that way, and a
    per-task cap plus a cooldown keep one task from eating the day's job budget. The owner's explicit
    approval bypasses the cap and the cooldown but still waits for a running job and for budget.
    """
    jobs_cfg = config.get("jobs") or {}
    if not jobs_cfg.get("enabled", True) and not is_native:
        return "disabled"
    if proposal.get("operation_id") in ("check", "inform"):
        return "check"  # nothing to run: a consistency question or a piece of news
    if proposal.get("operation_id") in EMAIL_SEND and not is_native:
        # A message to a third party (or a draft of one) runs ONLY through the deterministic native handler,
        # which performs exactly the approved operation. It never runs as a delegated agent that holds a send
        # tool and could email on its own — the failure that leaked a reply to a third party on 2026-10-27.
        return "no_safe_executor"
    task_id = proposal.get("task_id")
    if not task_id:
        return "no_task"
    try:
        task = store.get_task(task_id)
    except KeyError:
        return "no_task"
    if task.get("status") in TASK_TERMINAL:
        return "task_closed"
    if auto and proposal.get("action_class") == "external" and not may_auto_run(
        config.get("trust") or {}, "external", kind=task.get("kind"), operation_id=proposal.get("operation_id"),
    ):
        return "external_auto"  # only operations listed in trust.auto_external run unattended, never payments or deploys
    if store.list_jobs(statuses=("queued", "running"), task_id=task_id, limit=1):
        return "active_job"
    if is_native:
        return None  # no model, no subprocess: native actions are not capped or budgeted
    if auto:
        day_start = start_of_day(now_dt)
        today = [job for job in store.list_jobs(task_id=task_id, limit=50) if str(job.get("created_at") or "") >= day_start]
        if len(today) >= int(jobs_cfg.get("max_auto_per_task", 3)):
            return "task_cap"
        cooldown = int(jobs_cfg.get("auto_cooldown_minutes", 15))
        if cooldown > 0:
            since = iso(now_dt - timedelta(minutes=cooldown))
            if any(str(job.get("finished_at") or job.get("created_at") or "") >= since for job in today):
                return "cooldown"
    usage = store.budget_usage(now_dt.date().isoformat(), "jobs").get("jobs") or {}
    limit = int(config.get("limits", {}).get("jobs_per_day", 10))
    if int(usage.get("reserved", 0)) + int(usage.get("consumed", 0)) >= limit:
        return "budget"
    return None


def _grant(proposal: dict[str, Any], option: str, now: str) -> dict[str, Any]:
    response = proposal.get("response") or {}
    label = next(
        (str(o.get("label")) for o in proposal.get("options") or [] if isinstance(o, dict) and str(o.get("id")) == option),
        "",
    )
    return {
        "option": option, "label": label or option, "at": proposal.get("responded_at") or now,
        "actor": str(response.get("actor") or "owner"), "channel": str(response.get("channel") or ""),
        "note": str(response.get("note") or "")[:500],
    }


def enqueue_job_for_proposal(
    store: Any, config: dict[str, Any], *, proposal: dict[str, Any], option: str, now_dt: datetime, auto: bool = False,
) -> dict[str, Any] | None:
    """Queue the work a proposal describes. Returns the job, or None when :func:`job_block_reason` says no.

    Owner-approved external steps run as a delegated job that carries the exact approval (step, option,
    time) so the child agent performs that action and nothing else irreversible.
    """
    now = iso(now_dt)
    task_id = proposal.get("task_id")
    native_plan = None
    if task_id:
        try:
            native_plan = native.plan(store, proposal, store.get_task(task_id), option)
        except KeyError:
            native_plan = None
    reason = job_block_reason(store, config, proposal=proposal, now_dt=now_dt, auto=auto, is_native=native_plan is not None)
    if reason:
        if reason == "budget":
            store.add_audit(
                audit_id=str(uuid4()), event_type="budget", subject_type="proposal", subject_id=proposal.get("id"),
                correlation_id=task_id, actor_kind="scheduler", decision_code="budget_exhausted", metadata={"bucket": "jobs"}, now=now,
            )
        return None
    jobs_cfg = config.get("jobs") or {}
    task = store.get_task(task_id)
    if native_plan is not None:
        job = _enqueue_native(store, config, proposal=proposal, task=task, option=option, plan=native_plan, now_dt=now_dt, auto=auto)
        if job is not None:
            return job
    day = now_dt.date().isoformat()
    if not store.reserve_budget(
        bucket_start=day, resource_key="jobs", units=1, limit=int(config.get("limits", {}).get("jobs_per_day", 10)),
        now=now, bucket="jobs", unit="job",
    ):
        store.add_audit(
            audit_id=str(uuid4()), event_type="budget", subject_type="proposal", subject_id=proposal.get("id"),
            correlation_id=task_id, actor_kind="scheduler", decision_code="budget_exhausted", metadata={"bucket": "jobs"}, now=now,
        )
        return None
    store.settle_budget(bucket_start=day, resource_key="jobs", units=1, consumed=True, now=now, bucket="jobs")
    signals = store.signals_for_task(task_id, limit=5)
    facts = store.list_facts(limit=40)
    workdir = workdir_for(config, task)
    grant = None if auto else _grant(proposal, option, now)
    prompt = build_job_prompt(
        task=task, proposal=proposal, option=option, action_class=proposal.get("action_class", "read"),
        signals=signals, facts=facts, owner=config.get("owner", {}), workdir=workdir, grant=grant,
    )
    job_id = str(uuid4())
    store.create_job(
        job_id=job_id, task_id=task_id, proposal_id=proposal.get("id"), kind="proposal", prompt=prompt,
        action_class=proposal.get("action_class", "read"), now=now, workdir=workdir,
        worktree=bool(workdir and jobs_cfg.get("worktree", True)), skills=list(jobs_cfg.get("skills") or []),
        toolsets=list(jobs_cfg.get("toolsets") or []),
    )
    try:
        if proposal.get("status") in {"accepted", "waiting"}:
            store.set_proposal_status(proposal["id"], "executing", now)
    except (ValueError, RuntimeError, KeyError):
        pass
    try:
        if task.get("status") in {"open", "waiting", "needs_user", "snoozed"}:
            store.set_task_status(task_id, "in_progress", now=now, reason="job_started")
    except (ValueError, RuntimeError):
        pass
    store.add_audit(
        audit_id=str(uuid4()), event_type="job", subject_type="job", subject_id=job_id, correlation_id=task_id,
        actor_kind="scheduler" if auto else "verified_owner", decision_code="queued",
        metadata={"proposal_id": proposal.get("id"), "action_class": proposal.get("action_class"), "auto": auto,
                  "workdir": workdir or "", "grant": bool(grant)},
        now=now,
    )
    return store.get_job(job_id)


def _enqueue_native(store: Any, config: dict[str, Any], *, proposal: dict[str, Any], task: dict[str, Any], option: str,
                    plan: dict[str, Any], now_dt: datetime, auto: bool) -> dict[str, Any] | None:
    """A native job: the consent is recorded now (owner's button or trust policy) and consumed when it runs.
    Returns None when consent cannot be bound (no verified destination): the delegated path takes over."""
    now = iso(now_dt)
    operation, params = plan["operation"], plan["params"]
    grant = _grant(proposal, option, now)
    actor_ref = "policy:trust.auto_external" if auto else grant["actor"]
    channel = "policy" if auto else (grant["channel"] or "telegram")
    try:
        consent = native.grant(store, proposal=proposal, operation=operation, params=params, actor_ref=actor_ref, channel=channel,
                               destination_id=NATIVE_DESTINATION, hermes_home=config.get("_hermes_home"), now_dt=now_dt)
    except Exception as exc:
        log.info("native consent not recorded for %s (%s); delegating instead", proposal.get("id"), exc)
        store.add_audit(
            audit_id=str(uuid4()), event_type="consent", subject_type="proposal", subject_id=proposal.get("id"), correlation_id=task.get("id"),
            actor_kind="scheduler", decision_code="native_fallback", metadata={"operation": operation, "error": str(exc)[:160]}, now=now,
        )
        return None
    job_id = str(uuid4())
    store.create_job(
        job_id=job_id, task_id=task["id"], proposal_id=proposal.get("id"), kind=native.NATIVE_KIND, prompt=native.job_prompt(operation, params, consent),
        action_class=native.effect_class(operation), now=now,
    )
    try:
        if proposal.get("status") in {"accepted", "waiting"}:
            store.set_proposal_status(proposal["id"], "executing", now)
    except (ValueError, RuntimeError, KeyError):
        pass
    try:
        if task.get("status") in {"open", "waiting", "needs_user", "snoozed"}:
            store.set_task_status(task["id"], "in_progress", now=now, reason="job_started")
    except (ValueError, RuntimeError):
        pass
    store.add_audit(
        audit_id=str(uuid4()), event_type="job", subject_type="job", subject_id=job_id, correlation_id=task.get("id"),
        actor_kind="scheduler" if auto else "verified_owner", decision_code="queued",
        metadata={"proposal_id": proposal.get("id"), "action_class": native.effect_class(operation), "auto": auto, "native": operation,
                  "consent_id": consent["consent_id"], "plan_hash": consent["plan_hash"], "what": native.describe(operation, params)},
        now=now,
    )
    return store.get_job(job_id)


def expire_proposal(store: Any, proposal: dict[str, Any], reason: str, now: str) -> None:
    """Close a proposal that can never start, leaving the reason in the audit trail."""
    try:
        store.set_proposal_status(proposal["id"], "expired", now)
    except (ValueError, RuntimeError, KeyError):
        return
    store.add_audit(
        audit_id=str(uuid4()), event_type="job", subject_type="proposal", subject_id=proposal.get("id"),
        correlation_id=proposal.get("task_id"), actor_kind="scheduler", decision_code="not_started",
        metadata={"reason": reason, "action_class": proposal.get("action_class")}, now=now,
    )


def start_or_wait(
    store: Any, config: dict[str, Any], *, proposal: dict[str, Any], option: str, now_dt: datetime, auto: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Queue the job for an accepted (or waiting) proposal, or park it as ``waiting`` with the reason.

    Returns ``(job, None)`` when it started, ``(None, reason)`` otherwise. A proposal is never left
    ``accepted`` without a job: it waits (and the scheduler retries it every tick) or it expires with
    the reason audited when the block can never clear.
    """
    job = enqueue_job_for_proposal(store, config, proposal=proposal, option=option, now_dt=now_dt, auto=auto)
    if job is not None:
        return job, None
    reason = job_block_reason(store, config, proposal=proposal, now_dt=now_dt, auto=auto) or "not_queued"
    now = iso(now_dt)
    if reason in TERMINAL_BLOCKS or (auto and reason not in DEFER_REASONS):
        expire_proposal(store, proposal, reason, now)
        return None, reason
    if proposal.get("status") != "waiting":
        try:
            store.set_proposal_status(proposal["id"], "waiting", now)
        except (ValueError, RuntimeError, KeyError):
            return None, reason
    params = proposal.get("params") or {}
    store.update_proposal_params(proposal["id"], now=now, wait_reason=reason, wait_since=params.get("wait_since") or now)
    if (params.get("wait_reason") or "") != reason:
        store.add_audit(
            audit_id=str(uuid4()), event_type="job", subject_type="proposal", subject_id=proposal.get("id"),
            correlation_id=proposal.get("task_id"), actor_kind="scheduler" if auto else "verified_owner", decision_code="waiting",
            metadata={"reason": reason, "auto": auto}, now=now,
        )
    return None, reason


def record_job_outcome(store: Any, config: dict[str, Any], *, job: dict[str, Any], outcome: JobOutcome, now_dt: datetime) -> bool:
    """Persist the job's end state and emit a ``job_result`` signal for the pipeline.

    A job cancelled by the runtime (gateway restart, plugin reload) is not a result: its proposal goes
    back to ``waiting`` so the next tick starts it again, and no signal is emitted.
    """
    now = iso(now_dt)
    status = outcome.status if outcome.status in {"succeeded", "failed", "timed_out", "cancelled", "unknown"} else "failed"
    store.finish_job(
        job["id"], fencing_token=job["fencing_token"], status=status, summary=outcome.summary or None,
        result=outcome.result or {}, usage=outcome.usage or {}, error=outcome.error, now=now, session_ref=outcome.session_ref,
    )
    return _settle(store, config, job=job, status=status, outcome=outcome, now_dt=now_dt)


LOST_RETRIES = 1  # a job lost once (its lease ran out with nobody running it) starts again; lost twice, the task re-plans


def settle_lost_job(store: Any, config: dict[str, Any], *, job: dict[str, Any], now_dt: datetime) -> bool:
    """Close a running job whose lease ran out and that no task of this process runs (the gateway died under it):
    the first time its step waits to start again, after that it ends ``unknown`` and the task re-plans."""
    retry = bool(job.get("proposal_id")) and _attempts(store, job) <= LOST_RETRIES
    outcome = JobOutcome("cancelled" if retry else "unknown", error="lease expired")
    return record_job_outcome(store, config, job=job, outcome=outcome, now_dt=now_dt)


def release_orphans(store: Any, config: dict[str, Any], now_dt: datetime) -> int:
    """Steps left ``executing`` with no job queued or running (before 2026-10-25 a lost job never released its step,
    which stayed executing for good): settled as if their last job had just been lost. Returns how many."""
    released = 0
    for proposal in store.list_proposals(statuses=("executing",), limit=200):
        jobs = [j for j in store.list_jobs(task_id=proposal.get("task_id"), limit=200) if j.get("proposal_id") == proposal["id"]]
        if not jobs or any(j.get("status") in ("queued", "running") for j in jobs):
            continue
        last = max(jobs, key=lambda j: str(j.get("created_at") or ""))
        retry = len(jobs) <= LOST_RETRIES
        outcome = JobOutcome("cancelled" if retry else "unknown", error=last.get("error") or "lost")
        _settle(store, config, job=last, status=outcome.status, outcome=outcome, now_dt=now_dt)
        released += 1
    return released


def _attempts(store: Any, job: dict[str, Any]) -> int:
    return sum(1 for j in store.list_jobs(task_id=job.get("task_id"), limit=200) if j.get("proposal_id") == job.get("proposal_id"))


def _settle(store: Any, config: dict[str, Any], *, job: dict[str, Any], status: str, outcome: JobOutcome, now_dt: datetime) -> bool:
    """The step and the pipeline after a job ended: a cancelled job's step waits to start again (no signal); any
    other end settles the step and emits the ``job_result`` signal."""
    now = iso(now_dt)
    proposal_id = job.get("proposal_id")
    if status == "cancelled" and proposal_id:
        try:
            store.set_proposal_status(proposal_id, "waiting", now)
            store.update_proposal_params(proposal_id, now=now, wait_reason="interrupted", wait_since=now)
        except (ValueError, RuntimeError, KeyError):
            pass
        store.add_audit(
            audit_id=str(uuid4()), event_type="job", subject_type="proposal", subject_id=proposal_id,
            correlation_id=job.get("task_id"), actor_kind="scheduler", decision_code="waiting",
            metadata={"reason": "interrupted", "job_id": job["id"]}, now=now,
        )
        return False
    if proposal_id:
        try:
            store.set_proposal_status(proposal_id, "succeeded" if status == "succeeded" else "unknown" if status == "unknown" else "failed", now)
        except (ValueError, RuntimeError, KeyError):
            pass
    task_id = job.get("task_id")
    retention = config.get("retention", {})
    ensure_jobs_source(store, now, int(retention.get("observations_days", 30)))
    title = ""
    if proposal_id:
        try:
            title = store.get_proposal(proposal_id).get("title", "")
        except KeyError:
            title = ""
    summary = outcome.summary or outcome.error or status
    meta = {
        "task_id": task_id, "proposal_id": proposal_id, "job_id": job["id"], "status": status, "source_kind": "hermes",
        "action_class": job.get("action_class") or "read",
        "findings": list((outcome.result or {}).get("findings") or [])[:10],
        "refs": list((outcome.result or {}).get("refs") or [])[:10],
        "proposed_reply": str((outcome.result or {}).get("proposed_reply") or "")[:4000],
        "next_steps": list((outcome.result or {}).get("next_steps") or [])[:6],
        "effects": list((outcome.result or {}).get("effects") or [])[:10],
        "session_ref": outcome.session_ref or "",
        "native": str((outcome.result or {}).get("operation") or "") if job.get("kind") == native.NATIVE_KIND else "",
        "branch": str((outcome.result or {}).get("branch") or "")[:200], "repo": str((outcome.result or {}).get("repo") or "")[:200],
        "pr": str((outcome.result or {}).get("pr") or "")[:300], "base": str((outcome.result or {}).get("base") or "")[:100],
    }
    return store.add_signal(
        signal_id=str(uuid4()), source_id=JOBS_SOURCE_ID, kind="job_result", occurred_at=now,
        external_ref=f"job:{job['id']}", group_key=f"task:{task_id}" if task_id else f"job:{job['id']}",
        title=(f"[{status}] {title}" if title else f"[{status}] job")[:300], snippet=" ".join(str(summary).split())[:500],
        meta=meta, fingerprint=f"job:{job['id']}", now=now,
        raw_content=json.dumps(outcome.result or {}, ensure_ascii=False)[:20000] if outcome.result else None,
        raw_expires_at=iso(now_dt + timedelta(days=int(retention.get("full_content_days", 14)))) if outcome.result else None,
        expires_at=iso(now_dt + timedelta(days=int(retention.get("signals_days", 90)))),
    )

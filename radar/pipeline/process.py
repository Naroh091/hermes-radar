"""Process one claimed bundle of signals end to end."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..jobs.service import DEFER_REASONS, expire_proposal, job_block_reason, start_or_wait
from ..llm import BudgetExhausted, LlmError
from ..models import TASK_TERMINAL
from ..trust import may_auto_run
from .recall import build_query, recall_summary
from . import act, correlate, extract as extract_step, reason
from .triage import triage_bundle

log = logging.getLogger(__name__)

RETRY_MINUTES = 15
BUDGET_RETRY_HOURS = 6


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _synthetic_extract_from_job(signal: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    meta = signal.get("meta") or {}
    failed = meta.get("status") not in {"succeeded"}
    return {
        "relevant": True, "category": task.get("kind", "other"), "title": task.get("title", ""),
        "summary": signal.get("snippet", ""), "disposition": "digest" if failed else "notify_now",
        "reason": "job_result", "urgency": "low" if failed else "medium", "impact": "low", "risk_flags": [],
        "entities": {}, "expectations": [], "facts_learned": [], "proposed_reply": meta.get("proposed_reply", ""),
        "keep_full_content": False, "needs_thread": False,
        "job": {"status": meta.get("status"), "findings": meta.get("findings", []), "refs": meta.get("refs", []),
                "next_steps": meta.get("next_steps", []), "effects": meta.get("effects", []),
                "branch": meta.get("branch", ""), "repo": meta.get("repo", ""), "pr": meta.get("pr", ""), "base": meta.get("base", "")},
    }


def _settle_draft(store: Any, task_id: str, signal: dict[str, Any], now: str) -> None:
    """After a successful external mail step, the drafted reply is no longer pending."""
    meta = signal.get("meta") or {}
    if meta.get("status") != "succeeded" or not meta.get("proposal_id"):
        return
    try:
        proposal = store.get_proposal(meta["proposal_id"])
    except KeyError:
        return
    operation = str(proposal.get("operation_id") or "").lower()
    if proposal.get("action_class") == "external" and (operation.startswith("gmail.") or "send" in operation or "reply" in operation):
        act.forget_draft(store, task_id, now)
    if operation == "github.open_pr":
        refs = [str(r) for r in meta.get("refs") or [] if str(r).startswith("http")]
        act.remember_pr(store, task_id, refs[0] if refs else str(meta.get("pr") or ""), now)
    if operation == "cron.create":
        from ..patterns import mark_automated

        ref = next((str(r)[5:] for r in meta.get("refs") or [] if str(r).startswith("cron:")), "")
        mark_automated(store, task_id, ref, now)


def _owner_approved_job(store: Any, signal: dict[str, Any]) -> bool:
    """True when a job_result signal comes from a step the owner approved (not one the plugin chose)."""
    proposal_id = (signal.get("meta") or {}).get("proposal_id")
    if not proposal_id:
        return False
    try:
        response = store.get_proposal(proposal_id).get("response") or {}
    except KeyError:
        return False
    return not response.get("auto")


async def process_bundle(rt: Any, bundle: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    store, config, gate = rt.store, rt.config, rt.gate
    now = iso(now_dt)
    signals = bundle["signals"]
    owner = config.get("owner", {})
    preferences = store.list_preferences(active_only=True)
    summary: dict[str, Any] = {"group": bundle["group_key"], "signals": len(signals)}
    existing_task = None
    kept = signals
    if all(s.get("kind") in ("job_result", "cron_result") for s in signals) and signals:
        task_id = (signals[0].get("meta") or {}).get("task_id")
        if task_id:
            try:
                existing_task = store.get_task(task_id)
            except KeyError:
                existing_task = None
    if existing_task is None:
        kept, ignored = triage_bundle(signals, preferences, config)
        for signal, decision in ignored:
            store.finish_signals([signal], status="ignored", reason=decision.reason, now=now)
        summary["ignored"] = len(ignored)
        if not kept:
            return summary
    if not gate.available:
        store.defer_signals(kept, next_attempt_at=iso(now_dt + timedelta(minutes=RETRY_MINUTES)), error="llm unavailable", now=now)
        summary["deferred"] = "llm_unavailable"
        return summary
    max_attempts = int(config.get("limits", {}).get("signal_attempts", 5))
    if any(int(s.get("attempts") or 0) > max_attempts for s in kept):
        store.finish_signals(kept, status="error", reason="too_many_attempts", now=now)
        summary["error"] = "too_many_attempts"
        return summary
    facts = store.list_facts(limit=40)
    recall_job = asyncio.ensure_future(recall_context(rt, None, kept, existing_task))  # runs while the model extracts
    try:
        if existing_task is not None:
            extracted = _synthetic_extract_from_job(kept[-1], existing_task)
            candidates = []
        else:
            extracted = await extract_step.extract(
                gate, kept, owner=owner, now=now, facts=facts, open_tasks=store.open_task_summaries(), subject_id=kept[0]["id"],
            )
            if not extracted.get("relevant") or extracted.get("category") == "noise":
                store.finish_signals(kept, status="ignored", reason="model_noise", now=now, extracts={s["id"]: extracted for s in kept})
                _apply_retention(store, kept, keep=False, config=config, now_dt=now_dt)
                summary["result"] = "noise"
                return summary
            candidates = correlate.candidate_tasks(store, extracted)
        if existing_task is not None:
            existing_task = task_context(store, existing_task)
        recalled = await recall_job
        summary["recall"] = recall_summary(recalled)
        planned = await reason.plan(
            gate, now=now, owner=owner, trust_level=str(config.get("trust", {}).get("level", "balanced")),
            extract=extracted, signals=kept, candidates=candidates, facts=facts, preferences=preferences,
            existing_task=existing_task, subject_id=kept[0]["id"], recall=recalled,
        )
    except BudgetExhausted:
        recall_job.cancel()
        store.defer_signals(kept, next_attempt_at=iso(now_dt + timedelta(hours=BUDGET_RETRY_HOURS)), error="budget_exhausted", now=now)
        summary["deferred"] = "budget"
        return summary
    except LlmError as exc:
        recall_job.cancel()
        store.defer_signals(kept, next_attempt_at=iso(now_dt + timedelta(minutes=RETRY_MINUTES)), error=str(exc)[:200], now=now)
        summary["deferred"] = str(exc)[:100]
        return summary
    owner_followup = existing_task is not None and _owner_approved_job(store, kept[-1])
    result = act.apply(
        store, config, signals=[] if existing_task is not None else kept, extract=extracted, plan=planned,
        now_dt=now_dt, preferences=preferences, existing_task=existing_task, owner_followup=owner_followup,
    )
    if existing_task is not None:
        _settle_draft(store, existing_task["id"], kept[-1], now)
    if owner_followup and not result.proposals:
        from ..delivery.dispatch import notify_job_result

        summary["result_notice"] = notify_job_result(store, config, task_id=existing_task["id"], signal=kept[-1], now_dt=now_dt)
    if existing_task is not None:
        for signal in kept:
            try:
                store.link_task_signal(existing_task["id"], signal["id"], "advances", now)
            except Exception:
                pass
    _apply_retention(store, kept, keep=bool(extracted.get("keep_full_content")), config=config, now_dt=now_dt)
    store.finish_signals(kept, status="extracted", reason=result.decision, now=now, extracts={s["id"]: extracted for s in kept})
    auto_jobs = _auto_run(rt, result, now_dt)
    summary.update({
        "task_id": result.task_id, "decision": result.decision, "disposition": result.disposition,
        "proposals": len(result.proposals), "auto_jobs": auto_jobs, "facts": result.facts,
    })
    return summary


def _apply_retention(store: Any, signals: list[dict[str, Any]], *, keep: bool, config: dict[str, Any], now_dt: datetime) -> None:
    retention = config.get("retention", {})
    now = iso(now_dt)
    for signal in signals:
        if not signal.get("raw_content"):
            continue
        kinds = retention.get("full_content_kinds") or []
        if keep or signal.get("kind") in kinds:
            store.update_signal(signal["id"], now=now, raw_expires_at=iso(now_dt + timedelta(days=int(retention.get("full_content_days", 14)))))
        else:
            store.update_signal(signal["id"], now=now, raw_content=None, raw_expires_at=None)


async def recall_context(rt: Any, extract: dict[str, Any] | None, signals: list[dict[str, Any]], existing_task: dict[str, Any] | None) -> dict[str, Any] | None:
    """Memory and past-session context for the planner; None when recall is unavailable. Never raises."""
    recall = getattr(rt, "recall", None)
    if recall is None:
        return None
    try:
        recalled = await recall.gather(build_query(extract, signals, existing_task))
    except Exception as exc:
        log.info("recall skipped: %s", exc)
        return None
    try:
        status = rt.recall_status() if hasattr(rt, "recall_status") else {}
        log.info("recall for %s: memory=%d chars via %s/%s, sessions=%d%s",
                 (existing_task or {}).get("id") or (signals[0].get("id") if signals else "?"), len(recalled.get("memory") or ""),
                 status.get("provider") or "no provider", status.get("tool") or "-", len(recalled.get("past_sessions") or []),
                 f" (last error: {status['last_error']})" if status.get("last_error") else "")
    except Exception as exc:  # the summary line is cosmetic
        log.debug("recall summary skipped: %s", exc)
    return recalled


def task_context(store: Any, task: dict[str, Any]) -> dict[str, Any]:
    """The task plus what was already proposed and what the owner refused, for the planner."""
    task = dict(task)
    history = store.list_proposals(task_id=task["id"], limit=50)
    task["recent_proposals"] = [{"title": p.get("title"), "status": p.get("status")} for p in history[-8:]]
    task["declined_by_owner"] = [
        p.get("title") for p in history
        if p.get("status") == "declined" and not (p.get("response") or {}).get("auto") and not (p.get("response") or {}).get("task_action")
        and not (p.get("response") or {}).get("ack")
    ][-10:]
    return task


def _auto_run(rt: Any, result: Any, now_dt: datetime) -> int:
    """Start the auto-eligible proposals of a fresh plan: one runs, the others wait for it."""
    if not result.task_id or not result.proposals:
        return 0
    store = rt.store
    try:
        task = store.get_task(result.task_id)
    except KeyError:
        return 0
    if task.get("status") in TASK_TERMINAL or task.get("muted"):
        return 0
    started = 0
    for proposal_id in result.proposals:
        try:
            proposal = store.get_proposal(proposal_id)
        except KeyError:
            continue
        if _start_auto(rt, proposal, task, now_dt):
            started += 1
    return started


def _auto_eligible(rt: Any, proposal: dict[str, Any], task: dict[str, Any]) -> bool:
    if proposal.get("operation_id") in ("check", "inform"):
        return False
    trust = rt.config.get("trust") or {}
    return may_auto_run(trust, proposal.get("action_class", "read"), kind=task.get("kind"), operation_id=proposal.get("operation_id"))


def _start_auto(rt: Any, proposal: dict[str, Any], task: dict[str, Any], now_dt: datetime) -> bool:
    """Accept one pending proposal the trust level allows and start it, or park it as waiting.

    Caps that the owner could lift (per-task cap, jobs disabled) leave the proposal pending so it is
    asked instead of silently dropped.
    """
    store, config = rt.store, rt.config
    now = iso(now_dt)
    if proposal.get("status") != "pending" or not _auto_eligible(rt, proposal, task):
        return False
    params = proposal.get("params") or {}
    if params.get("deliver_after") and str(params["deliver_after"]) > now:
        return False  # scheduled for later on purpose
    reason = job_block_reason(store, config, proposal=proposal, now_dt=now_dt, auto=True)
    if reason and reason not in DEFER_REASONS:
        return False
    try:
        store.respond_proposal(
            proposal["id"], decision="accepted", response={"auto": True, "trust": (config.get("trust") or {}).get("level", "balanced")}, now=now,
        )
        accepted = store.get_proposal(proposal["id"])
    except (ValueError, RuntimeError, KeyError):
        return False
    job, _ = start_or_wait(store, config, proposal=accepted, option="do", now_dt=now_dt, auto=True)
    return job is not None


def _has_job(store: Any, proposal: dict[str, Any]) -> bool:
    return any(job.get("proposal_id") == proposal.get("id") for job in store.list_jobs(task_id=proposal.get("task_id"), limit=50))


def _hand_back(store: Any, proposal: dict[str, Any], now: str) -> None:
    """Trust was lowered while an automatic step waited: let the owner decide instead."""
    try:
        store.set_proposal_status(proposal["id"], "pending", now)
        store.update_proposal_params(proposal["id"], now=now, wait_reason=None, wait_since=None)
    except (ValueError, RuntimeError, KeyError):
        pass


def retry_pending_jobs(rt: Any, now_dt: datetime) -> int:
    """Each tick: start waiting steps whose block cleared; park accepted steps that never got a job."""
    store, config = rt.store, rt.config
    now = iso(now_dt)
    started = 0
    for proposal in store.list_proposals(statuses=("waiting", "accepted"), limit=200):
        response = proposal.get("response") or {}
        auto = bool(response.get("auto"))
        task_id = proposal.get("task_id")
        try:
            task = store.get_task(task_id) if task_id else None
        except KeyError:
            task = None
        if task is None or task.get("status") in TASK_TERMINAL or task.get("muted") or str(proposal.get("expires_at") or "") <= now:
            expire_proposal(store, proposal, "stale", now)
            continue
        if proposal.get("status") == "accepted" and _has_job(store, proposal):
            continue  # queued already; only the status update was lost
        if auto and not _auto_eligible(rt, proposal, task):
            _hand_back(store, proposal, now)
            continue
        job, _ = start_or_wait(store, config, proposal=proposal, option=str(response.get("option") or "do"), now_dt=now_dt, auto=auto)
        if job is not None:
            started += 1
    return started


async def review_task(rt: Any, task: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    """Re-plan an existing task when its review timer fires (no new signal)."""
    store, config, gate = rt.store, rt.config, rt.gate
    now = iso(now_dt)
    summary: dict[str, Any] = {"task_id": task["id"]}
    if task.get("status") in TASK_TERMINAL or task.get("muted"):
        summary["skipped"] = "closed_or_muted"
        return summary
    if not gate.available:
        summary["skipped"] = "llm_unavailable"
        return summary
    preferences = store.list_preferences(active_only=True)
    facts = store.list_facts(limit=40)
    signals = store.signals_for_task(task["id"], limit=5)
    task = task_context(store, task)
    recalled = await recall_context(rt, None, signals, task)
    summary["recall"] = recall_summary(recalled)
    try:
        planned = await reason.plan(
            gate, now=now, owner=config.get("owner", {}), trust_level=str(config.get("trust", {}).get("level", "balanced")),
            extract=None, signals=signals, candidates=[], facts=facts, preferences=preferences,
            existing_task=task, subject_id=task["id"], recall=recalled,
        )
    except LlmError as exc:
        summary["deferred"] = str(exc)[:100]
        return summary
    result = act.apply(store, config, signals=[], extract=None, plan=planned, now_dt=now_dt, preferences=preferences, existing_task=task)
    summary.update({"decision": result.decision, "disposition": result.disposition, "proposals": len(result.proposals),
                    "auto_jobs": _auto_run(rt, result, now_dt), "status": result.task_status})
    return summary

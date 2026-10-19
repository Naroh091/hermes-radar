"""Host LLM access with daily budgets, audit and schema-bound answers.

Every call goes through ``ctx.llm`` (``acomplete_structured``/``complete_structured``) and a
plugin-owned auxiliary task slot (``auxiliary.radar_<slot>`` in config), so the operator can
pin a cheap model per step without granting model overrides.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

log = logging.getLogger(__name__)

AUX_PREFIX = "radar_"
SLOTS: dict[str, tuple[str, str]] = {
    "extract": ("Radar: extract", "Structured extraction and triage of one bundle of signals (email, chat turns, events)."),
    "plan": ("Radar: plan", "Task correlation, checklist gaps and proposals for one task."),
    "intent": ("Radar: intent", "Classify a free-text owner reply to a notice."),
    "preference": ("Radar: preferences", "Turn one owner reply into a scoped attention preference."),
    "pattern": ("Radar: patterns", "Name recurring patterns and propose automations."),
    "card": ("Radar: cards", "Fill presentation templates for the desktop panel."),
}


class LlmError(RuntimeError):
    """The model answer could not be trusted or the call failed; callers retry later."""


class BudgetExhausted(LlmError):
    """The daily LLM budget for a slot is spent; callers defer, never skip silently."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def register_aux_tasks(ctx: Any) -> list[str]:
    registered: list[str] = []
    register = getattr(ctx, "register_auxiliary_task", None)
    if not callable(register):
        return registered
    for slot, (name, description) in SLOTS.items():
        key = AUX_PREFIX + slot
        try:
            register(key, display_name=name, description=description)
            registered.append(key)
        except Exception as exc:  # already registered on reload, or host without aux tasks
            log.debug("auxiliary task %s not registered: %s", key, exc)
    return registered


class LlmGate:
    def __init__(self, ctx: Any, store: Any, config: dict[str, Any], *, clock: Callable[[], datetime] = utc_now):
        self.ctx = ctx
        self.store = store
        self.config = config
        self.clock = clock

    @property
    def available(self) -> bool:
        return getattr(self.ctx, "llm", None) is not None

    async def structured(
        self, slot: str, *, instructions: str, input_text: str, schema: dict[str, Any], purpose: str,
        temperature: float = 0.0, max_tokens: int | None = None, subject_id: str | None = None,
    ) -> dict[str, Any]:
        if slot not in SLOTS:
            raise ValueError(f"unknown llm slot {slot}")
        llm = getattr(self.ctx, "llm", None)
        if llm is None:
            raise LlmError("host does not expose ctx.llm")
        now_dt = self.clock()
        now = iso(now_dt)
        day = now_dt.date().isoformat()
        limit = int(self.config.get("limits", {}).get("llm_calls_per_day", 200))
        if not self.store.reserve_budget(
            bucket_start=day, resource_key=slot, units=1, limit=limit, now=now, bucket="llm", unit="call",
        ):
            self.store.add_audit(
                audit_id=str(uuid4()), event_type="budget", subject_type="llm", subject_id=subject_id,
                correlation_id=None, actor_kind="scheduler", decision_code="budget_exhausted",
                metadata={"slot": slot, "purpose": purpose}, now=now,
            )
            raise BudgetExhausted(slot)
        called = False
        kwargs: dict[str, Any] = {
            "instructions": instructions,
            "input": [{"type": "text", "text": input_text}],
            "json_schema": schema,
            "purpose": purpose,
            "temperature": temperature,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        try:
            result = await self._call(llm, slot, kwargs)
            called = True
            parsed = getattr(result, "parsed", None)
            if parsed is None:
                text = getattr(result, "text", "") or ""
                try:
                    parsed = json.loads(text)
                except ValueError as exc:
                    raise LlmError("model returned no structured answer") from exc
            if not isinstance(parsed, dict):
                raise LlmError("structured answer is not an object")
            self._audit(result, slot=slot, purpose=purpose, subject_id=subject_id, now=iso(self.clock()))
            return parsed
        finally:
            self.store.settle_budget(
                bucket_start=day, resource_key=slot, units=1, consumed=called, now=iso(self.clock()), bucket="llm",
            )

    async def _call(self, llm: Any, slot: str, kwargs: dict[str, Any]) -> Any:
        task = AUX_PREFIX + slot
        acomplete = getattr(llm, "acomplete_structured", None)
        for attempt_task in (task, None):
            call_kwargs = dict(kwargs)
            if attempt_task:
                call_kwargs["task"] = attempt_task
            try:
                if callable(acomplete):
                    return await acomplete(**call_kwargs)
                return await asyncio.to_thread(llm.complete_structured, **call_kwargs)
            except TypeError as exc:
                # Fakes or older hosts without ``task``: fall through to a plain call.
                if attempt_task and "task" in str(exc):
                    continue
                raise LlmError(f"llm call failed: {exc}") from exc
            except Exception as exc:
                name = type(exc).__name__
                if attempt_task and ("Trust" in name or "task" in str(exc).lower()):
                    log.debug("aux task %s refused (%s); retrying with the main model", attempt_task, exc)
                    continue
                raise LlmError(f"llm call failed: {name}: {str(exc)[:200]}") from exc
        raise LlmError("llm call failed")

    def _audit(self, result: Any, *, slot: str, purpose: str, subject_id: str | None, now: str) -> None:
        usage = getattr(result, "usage", None)
        metadata = {
            "slot": slot,
            "purpose": purpose,
            "model": str(getattr(result, "model", "") or ""),
            "provider": str(getattr(result, "provider", "") or ""),
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }
        cost = getattr(usage, "cost_usd", None)
        if isinstance(cost, (int, float)):
            metadata["cost_usd"] = float(cost)
        try:
            self.store.add_audit(
                audit_id=str(uuid4()), event_type="llm_call", subject_type="llm", subject_id=subject_id,
                correlation_id=None, actor_kind="scheduler", decision_code="completed", metadata=metadata, now=now,
            )
        except Exception as exc:  # audit must never break the pipeline
            log.debug("llm audit skipped: %s", exc)

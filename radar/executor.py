from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from .models import canonical_hash
from .scheduler import iso, utc_now
from .store import Store


class MutationExecutor:
    """Single authority path for consent-bound mutation operations."""

    def __init__(
        self,
        store: Store,
        operations: dict[str, Any],
        *,
        worker_id: str,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.operations = operations
        self.worker_id = worker_id
        self.clock = clock

    async def execute(
        self, *, plan_id: str, consent_id: str, actor_ref: str,
        destination_id: str, verified_transport: str,
    ) -> dict[str, Any]:
        plan = self.store.inspect_object(plan_id)
        if plan.get("object_type") != "plan" or plan.get("effect_class") != "external":
            raise ValueError("plan is not a mutation")
        operation = self.operations.get(plan["operation_id"])
        if operation is None or operation.effect_class != "external":
            raise ValueError("mutation operation is not registered")
        parameters = plan.get("parameters")
        operation.validate_parameters(parameters)
        now_dt = self.clock()
        now = iso(now_dt)
        # Consent is consumed before provider access. Any retry requires reconciliation or a new consent.
        self.store.consume_consent(
            consent_id, plan_hash=plan["plan_hash"], actor_ref=actor_ref,
            destination_id=destination_id, verified_transport=verified_transport, now=now,
        )
        execution_key = canonical_hash({"plan_hash": plan["plan_hash"], "consent_id": consent_id})
        run = self.store.create_run(
            run_id=str(uuid4()), followup_id=plan["followup_id"], plan_id=plan_id,
            execution_key=execution_key, effect_class="external", now=now,
        )
        if not run["created"]:
            return {"run_id": run["id"], "status": run["status"], "reused": True}
        fence = self.store.start_run(
            run["id"], worker_id=self.worker_id,
            lease_until=iso(now_dt + timedelta(minutes=2)), now=now,
        )
        try:
            result = await asyncio.to_thread(
                operation.execute,
                self.store.scope,
                parameters,
                {"idempotency_key": execution_key},
            )
            terminal = result.status if result.status in {"succeeded", "failed", "unknown"} else "unknown"
            self.store.finish_run(
                run["id"], fencing_token=fence, status=terminal, result=result.data,
                error_code=None if terminal == "succeeded" else f"mutation_{terminal}",
                now=iso(self.clock()),
            )
            self.store.add_audit(
                audit_id=str(uuid4()), event_type="mutation_finished", subject_type="run",
                subject_id=run["id"], correlation_id=execution_key, actor_kind="verified_owner",
                decision_code=terminal,
                metadata={"operation_id": plan["operation_id"], "consent_id": consent_id},
                now=iso(self.clock()),
            )
            return {"run_id": run["id"], "status": terminal, "reused": False}
        except Exception as exc:
            # A provider exception after dispatch may be uncertain. Never retry blindly.
            self.store.finish_run(
                run["id"], fencing_token=fence, status="unknown",
                result={"error": str(exc)[:500]}, error_code="mutation_uncertain",
                now=iso(self.clock()),
            )
            return {"run_id": run["id"], "status": "unknown", "reused": False}

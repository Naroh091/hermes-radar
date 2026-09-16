from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from .models import canonical_hash
from .operations import OPERATIONS, OperationResult
from .policy import Policy
from .store import Store
from .transports import TransportRegistry

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sync_configured_sources(store: Store, config: dict[str, Any], now: datetime | None = None) -> list[dict[str, str]]:
    """Validate and materialize configured HTTPS monitors, grants, goals, and follow-ups."""
    current = now or utc_now()
    stamp = iso(current)
    synced: list[dict[str, str]] = []
    for raw in config["sources"]:
        if not isinstance(raw, dict):
            raise ValueError("each source must be an object")
        source_id = raw["id"]
        if raw["kind"] != "http_status":
            raise ValueError(f"unsupported source kind: {raw['kind']}")
        url = raw["url"]
        parameters = {"url": url}
        OPERATIONS["http_status.check"].validate_parameters(parameters)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443):
            raise ValueError("http_status sources require an HTTPS URL on port 443")
        host = parsed.hostname.lower().rstrip(".")
        configured_operations = raw.get("operations", ["http_status.check"])
        operations = [op for op in configured_operations if op in config["allowed_operations"]]
        if "http_status.check" not in operations:
            raise ValueError(f"source {source_id} requires allowed operation http_status.check")
        synced.append(
            store.sync_http_source(
                source_id=source_id,
                url=url,
                host=host,
                interval_seconds=raw.get("interval_seconds", 300),
                retention_days=raw.get("retention_days", config["retention"]["observations_days"]),
                enabled=raw.get("enabled", True),
                allowed_operations=operations,
                now=stamp,
            )
        )
    return synced


class Scheduler:
    """Claims due follow-ups and executes closed-catalog informational operations."""

    def __init__(
        self,
        store: Store,
        config: dict[str, Any],
        *,
        worker_id: str,
        operations: dict[str, Any] | None = None,
        transports: TransportRegistry | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.config = config
        self.worker_id = worker_id
        self.operations = operations or OPERATIONS
        self.transports = transports or TransportRegistry()
        self.clock = clock
        self._semaphore = asyncio.Semaphore(config["scheduler"]["max_concurrent_checks"])

    async def tick(self) -> dict[str, int]:
        now_dt = self.clock()
        now = iso(now_dt)
        stale_before = iso(now_dt - timedelta(seconds=max(60, self.config["scheduler"]["tick_seconds"] * 5)))
        recovered = self.store.recover_stale_checks(stale_before, now)
        due = self.store.claim_due_followups(now, self.config["scheduler"]["max_concurrent_checks"])
        results = await asyncio.gather(*(self._process(item, now_dt) for item in due), return_exceptions=True)
        failed = 0
        for result in results:
            if isinstance(result, Exception):
                failed += 1
                log.exception("proactive scheduled check failed", exc_info=result)
        delivered = await self._deliver_outbox(now_dt)
        self.store.prune_expired(now)
        return {"claimed": len(due), "failed": failed, "recovered": recovered, "delivered": delivered}

    async def _process(self, item: dict[str, Any], now_dt: datetime) -> None:
        async with self._semaphore:
            now = iso(now_dt)
            followup_id = item["id"]
            assumptions = item.get("assumptions") or {}
            source_id = assumptions.get("source_id")
            operation_id = assumptions.get("operation_id")
            parameters = assumptions.get("parameters")
            interval = assumptions.get("interval_seconds", 300)
            next_at = iso(now_dt + timedelta(seconds=max(1, int(interval))))
            policy = Policy(self.store, self.config["allowed_operations"])
            decision = policy.authorize_informational(operation_id, source_id, parameters, now)
            if not decision.allowed or decision.permission_revision is None:
                self.store.add_audit(
                    audit_id=str(uuid4()), event_type="authorization", subject_type="followup",
                    subject_id=followup_id, correlation_id=None, actor_kind="scheduler",
                    decision_code=decision.code, metadata={"operation_id": operation_id}, now=now,
                )
                self.store.finish_followup_check(followup_id, success=False, next_check_at=next_at, now=now)
                return
            operation = self.operations[operation_id]
            day = now_dt.date().isoformat()
            reserved = self.store.reserve_budget(
                bucket_start=day, resource_key=operation_id, units=operation.budget_units,
                limit=self.config["limits"]["checks_per_day"], now=now,
            )
            if not reserved:
                self.store.add_audit(
                    audit_id=str(uuid4()), event_type="budget", subject_type="followup",
                    subject_id=followup_id, correlation_id=None, actor_kind="scheduler",
                    decision_code="budget_exhausted", metadata={"operation_id": operation_id}, now=now,
                )
                self.store.finish_followup_check(followup_id, success=True, next_check_at=next_at, now=now)
                return
            settled = False
            try:
                plan = self.store.add_plan(
                    plan_id=str(uuid4()), followup_id=followup_id, source_id=source_id,
                    operation_id=operation_id, operation_version=operation.version,
                    parameters=parameters, effect_class=operation.effect_class,
                    evidence_revision=item["evidence_revision"],
                    permission_revision=decision.permission_revision, now=now,
                )
                execution_key = canonical_hash(
                    {"plan_hash": plan["plan_hash"], "due_at": item.get("next_check_at") or now}
                )
                run = self.store.create_run(
                    run_id=str(uuid4()), followup_id=followup_id, plan_id=plan["id"],
                    execution_key=execution_key, effect_class=operation.effect_class, now=now,
                )
                if not run["created"]:
                    self.store.settle_budget(
                        bucket_start=day, resource_key=operation_id, units=operation.budget_units,
                        consumed=False, now=now,
                    )
                    settled = True
                    self.store.finish_followup_check(followup_id, success=True, next_check_at=next_at, now=now)
                    return
                fence = self.store.start_run(
                    run["id"], worker_id=self.worker_id, lease_until=iso(now_dt + timedelta(minutes=2)), now=now
                )
                host = operation.resource_identity(parameters)[1]
                result: OperationResult = await asyncio.to_thread(
                    operation.execute, self.store.scope, parameters, {"allowlist": [host]}
                )
                terminal = "succeeded" if result.status == "succeeded" else "failed"
                self.store.finish_run(
                    run["id"], fencing_token=fence, status=terminal, result=result.data,
                    error_code=None if terminal == "succeeded" else "operation_failed", now=iso(self.clock()),
                )
                self.store.settle_budget(
                    bucket_start=day, resource_key=operation_id, units=operation.budget_units,
                    consumed=True, now=iso(self.clock()),
                )
                settled = True
                self.store.add_audit(
                    audit_id=str(uuid4()), event_type="run_finished", subject_type="run",
                    subject_id=run["id"], correlation_id=execution_key, actor_kind="scheduler",
                    decision_code=terminal, metadata={"operation_id": operation_id}, now=iso(self.clock()),
                )
                await self._maybe_enqueue_notification(
                    run_id=run["id"], followup_id=followup_id, operation_id=operation_id,
                    parameters=parameters, terminal=terminal, result=result.data, now_dt=self.clock(),
                )
                # A failed read is retried at the source interval. It does not reopen a terminal run.
                self.store.finish_followup_check(
                    followup_id, success=True, next_check_at=next_at, now=iso(self.clock())
                )
            except Exception:
                if not settled:
                    self.store.settle_budget(
                        bucket_start=day, resource_key=operation_id, units=operation.budget_units,
                        consumed=False, now=iso(self.clock()),
                    )
                # The claim remains recoverable if persistence failed before this update.
                try:
                    self.store.finish_followup_check(
                        followup_id, success=True, next_check_at=next_at, now=iso(self.clock())
                    )
                except Exception:
                    pass
                raise

    async def _maybe_enqueue_notification(
        self, *, run_id: str, followup_id: str, operation_id: str,
        parameters: dict[str, Any], terminal: str, result: dict[str, Any], now_dt: datetime,
    ) -> None:
        if self.config["mode"] != "active" or not self.config["notifications"]["enabled"]:
            return
        unhealthy = terminal != "succeeded" or int(result.get("http_status", 200)) >= 400
        if not unhealthy:
            return
        start_of_day = now_dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if self.store.notification_count_since(iso(start_of_day)) >= self.config["notifications"]["daily_limit"]:
            return
        try:
            self.store.destination("configured:primary")
        except KeyError:
            return
        status_text = result.get("http_status", terminal)
        self.store.enqueue_notification(
            outbox_id=str(uuid4()), destination_id="configured:primary", followup_id=followup_id,
            proposal_id=None, notification_key=f"run:{run_id}:result",
            payload={
                "text": f"Proactive: {parameters.get('url')} ha devuelto {status_text}.",
                "run_id": run_id,
                "operation_id": operation_id,
            },
            now=iso(now_dt),
        )

    def _quiet(self, now_dt: datetime) -> bool:
        try:
            local = now_dt.astimezone(ZoneInfo(self.config["owner"]["timezone"]))
            start_h, start_m = map(int, self.config["notifications"]["quiet_start"].split(":"))
            end_h, end_m = map(int, self.config["notifications"]["quiet_end"].split(":"))
        except Exception:
            return True
        minute = local.hour * 60 + local.minute
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        return start <= minute < end if start < end else minute >= start or minute < end

    async def _deliver_outbox(self, now_dt: datetime) -> int:
        if self.config["mode"] != "active" or not self.config["notifications"]["enabled"]:
            return 0
        if self._quiet(now_dt):
            return 0
        now = iso(now_dt)
        rows = self.store.claim_outbox(now, iso(now_dt + timedelta(minutes=2)), limit=10)
        delivered = 0
        for row in rows:
            try:
                destination = self.store.destination(row["destination_id"])
                receipt = await self.transports.send(row["platform"], destination, row["payload"])
                self.store.finish_outbox(
                    row["id"], fencing_token=row["fencing_token"], success=True,
                    platform_message_id=receipt.platform_message_id, error=None,
                    next_attempt_at=now, now=iso(self.clock()),
                )
                delivered += 1
            except Exception as exc:
                delay = min(3600, 2 ** min(row["attempt"] + 1, 10))
                self.store.finish_outbox(
                    row["id"], fencing_token=row["fencing_token"], success=False,
                    platform_message_id=None, error=str(exc)[:500],
                    next_attempt_at=iso(now_dt + timedelta(seconds=delay)), now=iso(self.clock()),
                )
        return delivered

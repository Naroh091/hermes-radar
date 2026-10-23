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

SUPPORTED_SOURCE_KINDS = ("http_status", "gmail", "calendar", "github")
WATCH_RENEWAL_HOURS = 24


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sync_configured_sources(store: Store, config: dict[str, Any], now: datetime | None = None) -> list[dict[str, str]]:
    """Validate and materialize configured sources, grants and timers."""
    current = now or utc_now()
    stamp = iso(current)
    synced: list[dict[str, str]] = []
    active_source_ids: set[str] = set()
    retention_days = int(config["retention"]["observations_days"])
    for raw in config["sources"]:
        if not isinstance(raw, dict):
            raise ValueError("each source must be an object")
        source_id = raw["id"]
        kind = raw.get("kind")
        if kind not in SUPPORTED_SOURCE_KINDS:
            raise ValueError(f"unsupported source kind: {kind}")
        enabled = raw.get("enabled", True)
        if enabled:
            active_source_ids.add(f"configured:{source_id}")
        if kind == "http_status":
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
                    source_id=source_id, url=url, host=host, interval_seconds=raw.get("interval_seconds", 300),
                    retention_days=raw.get("retention_days", retention_days), enabled=enabled,
                    allowed_operations=operations, now=stamp,
                )
            )
            continue
        account_ref = str(raw.get("address") or raw.get("calendar_id") or raw.get("server") or source_id)
        resource_scope = {"accounts": [account_ref]} if kind == "gmail" else {"repos": list(raw.get("repos") or [])} if kind == "github" else {"calendars": [account_ref]}
        operations = {"gmail": ["gmail.read"], "calendar": ["calendar.read"], "github": ["repo.read"]}[kind]
        result = store.sync_source(
            source_id=source_id, kind=kind, account_ref=account_ref, config=raw, resource_scope=resource_scope,
            retention_days=int(raw.get("retention_days", retention_days)), enabled=bool(enabled), operations=operations, now=stamp,
        )
        push = raw.get("push") or {}
        if kind == "gmail" and push.get("enabled") and push.get("topic") and push.get("credentials_path"):
            store.upsert_followup(
                followup_id=f"routine:gmail-watch:{source_id}", goal_id="routines", goal_title="Routines", reason="routine",
                subject_key=f"routine:gmail-watch:{source_id}", next_check_at=stamp, expires_at="9999-12-31T23:59:59Z",
                assumptions={"kind": "routine", "routine": "gmail_watch", "source_id": result["source_id"], "config_source_id": source_id},
                now=stamp, status="ready" if enabled else "paused",
            )
        synced.append(result)
    store.disable_missing_configured_sources(active_source_ids, stamp)
    ensure_routines(store, stamp)
    return synced


def ensure_routines(store: Any, now: str) -> None:
    """Standing timers that do not belong to a source: the daily pattern mining (first run an hour after
    install, then every 24 h from each run; an existing timer keeps its own schedule). Also repairs tasks
    born from a chat before session ids were recorded, so they stay out of live conversations."""
    try:
        fixed = store.backfill_task_sessions(now)
        if fixed:
            log.info("radar: %d task(s) now remember their chat session", fixed)
    except Exception as exc:
        log.debug("task session backfill skipped: %s", exc)
    try:
        if any(row.get("id") == "routine:pattern-mining" for row in store.list_followups(limit=100)):
            return
        first = iso(datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(hours=1))
        store.upsert_followup(
            followup_id="routine:pattern-mining", goal_id="routines", goal_title="Routines", reason="routine", subject_key="routine:pattern-mining",
            next_check_at=first, expires_at="9999-12-31T23:59:59Z", assumptions={"kind": "routine", "routine": "pattern_mining"}, now=now, status="ready",
        )
    except Exception as exc:
        log.debug("pattern mining routine not armed: %s", exc)


class Scheduler:
    """Claims due timers, syncs sources, processes signals, runs jobs and delivers notices."""

    def __init__(
        self,
        store: Store,
        config: dict[str, Any],
        *,
        worker_id: str,
        operations: dict[str, Any] | None = None,
        transports: TransportRegistry | None = None,
        clock: Callable[[], datetime] = utc_now,
        runtime: Any = None,
    ) -> None:
        self.store = store
        self.config = config
        self.worker_id = worker_id
        self.operations = operations or OPERATIONS
        self.transports = transports or TransportRegistry()
        self.clock = clock
        self.runtime = runtime
        self._semaphore = asyncio.Semaphore(config["scheduler"]["max_concurrent_checks"])
        self._sync_locks: dict[str, asyncio.Lock] = {}

    async def tick(self) -> dict[str, Any]:
        if self.runtime is None:
            return await self._tick()
        from .hostscope import profile_secret_scope

        # Provider credentials (ctx.llm, MCP) are profile-scoped on a multiplexed gateway.
        with profile_secret_scope(getattr(self.runtime, "hermes_home", None)):
            return await self._tick()

    async def _tick(self) -> dict[str, Any]:
        now_dt = self.clock()
        now = iso(now_dt)
        stats: dict[str, Any] = {"claimed": 0, "failed": 0, "recovered": 0, "delivered": 0}
        if self.runtime is not None:
            stats["config_reloaded"] = bool(self.runtime.refresh_config())
        stale_before = iso(now_dt - timedelta(seconds=max(60, self.config["scheduler"]["tick_seconds"] * 5)))
        stats["recovered"] = self.store.recover_stale_checks(stale_before, now)
        paused = bool(self.runtime is not None and getattr(self.runtime, "paused", False))
        if not paused:
            due = self.store.claim_due_followups(now, self.config["scheduler"]["max_concurrent_checks"])
            results = await asyncio.gather(*(self._dispatch(item, now_dt) for item in due), return_exceptions=True)
            stats["claimed"] = len(due)
            for result in results:
                if isinstance(result, Exception):
                    stats["failed"] += 1
                    log.exception("radar scheduled check failed", exc_info=result)
            if self.runtime is not None:
                stats["synced"] = await self._sync_sources(now_dt)
                stats["bundles"] = await self._process_signals(now_dt)
                stats["retried"] = self._retry_jobs(now_dt)
                stats["jobs"] = await self._run_jobs(now_dt)
        stats["expired"] = self.store.expire_proposals(now)
        self.store.wake_snoozed_proposals(now)
        if self.runtime is not None and not paused:
            from .delivery.dispatch import dispatch, select_reminder
            from .delivery.remind import compose

            try:
                stats["refreshed"] = await self.runtime.refresh_artifacts(now_dt)
            except Exception as exc:
                log.warning("radar refresh before digest failed: %s", exc)
            reminder = None
            try:
                reminder = select_reminder(self.store, self.config, now_dt)
                if reminder:  # the model writes it as an assistant would; the template stands in when it cannot
                    reminder["text"] = await compose(
                        getattr(self.runtime, "gate", None), items=reminder["items"], language=self.config.get("owner", {}).get("language", "es"),
                        backlog=reminder["backlog"], first_of_day=reminder["first_of_day"],
                        subject_id=(reminder["items"][0].get("task") or {}).get("id") if reminder["items"] else None,
                    )
            except Exception as exc:
                log.warning("radar reminder not composed: %s", exc)
            try:
                stats["dispatched"] = dispatch(self.store, self.config, now_dt, reminder=reminder)
            except Exception as exc:
                log.warning("radar dispatch failed: %s", exc)
        stats["delivered"] = await self._deliver_outbox(now_dt)
        audit_before = iso(now_dt - timedelta(days=int(self.config["retention"].get("audit_days", 90))))
        self.store.prune_expired(now, audit_before=audit_before)
        return stats

    # -- timers -----------------------------------------------------------------------------------
    async def _dispatch(self, item: dict[str, Any], now_dt: datetime) -> None:
        kind = (item.get("assumptions") or {}).get("kind", "operation")
        if kind == "task_review":
            await self._review_task(item, now_dt)
        elif kind == "routine":
            await self._routine(item, now_dt)
        else:
            await self._process(item, now_dt)

    async def _review_task(self, item: dict[str, Any], now_dt: datetime) -> None:
        now = iso(now_dt)
        task_id = (item.get("assumptions") or {}).get("task_id") or item.get("task_id")
        try:
            task = self.store.get_task(task_id)
        except KeyError:
            self.store.finish_followup_check(item["id"], success=True, next_check_at=None, now=now, complete=True)
            return
        if self.runtime is None:
            self.store.finish_followup_check(item["id"], success=True, next_check_at=iso(now_dt + timedelta(hours=6)), now=now)
            return
        from .pipeline.process import review_task

        try:
            await review_task(self.runtime, task, now_dt)
        except Exception as exc:
            log.warning("task review failed for %s: %s", task_id, exc)
            self.store.finish_followup_check(item["id"], success=True, next_check_at=iso(now_dt + timedelta(hours=6)), now=iso(self.clock()))
            return
        refreshed = self.store.get_task(task_id)
        next_review = refreshed.get("next_review_at")
        if refreshed.get("status") in {"done", "abandoned", "expired"} or not next_review or next_review <= now:
            self.store.finish_followup_check(item["id"], success=True, next_check_at=None, now=iso(self.clock()), complete=True)
        else:
            self.store.finish_followup_check(item["id"], success=True, next_check_at=next_review, now=iso(self.clock()))

    async def _routine(self, item: dict[str, Any], now_dt: datetime) -> None:
        assumptions = item.get("assumptions") or {}
        routine = assumptions.get("routine")
        now = iso(now_dt)
        next_at = iso(now_dt + timedelta(hours=WATCH_RENEWAL_HOURS))
        if routine == "gmail_watch":
            source_cfg = next((s for s in self.config.get("sources", []) if s.get("id") == assumptions.get("config_source_id")), None)
            push = (source_cfg or {}).get("push") or {}
            try:
                from .ingress.gmail_watch import renew_watch

                result = await asyncio.to_thread(renew_watch, push.get("credentials_path"), push.get("topic"), source_cfg.get("labels") or ["INBOX"])
                self.store.update_source_health(assumptions["source_id"], {"watch": "ok", "history_id": result["history_id"], "expiration_ms": result["expiration_ms"], "at": now}, now)
            except Exception as exc:
                next_at = iso(now_dt + timedelta(hours=1))
                self.store.update_source_health(assumptions["source_id"], {"watch": "error", "error": str(exc)[:300], "at": now}, now)
                self.store.add_audit(
                    audit_id=str(uuid4()), event_type="routine", subject_type="source", subject_id=assumptions.get("source_id"),
                    correlation_id=None, actor_kind="scheduler", decision_code="gmail_watch_failed", metadata={"error": str(exc)[:200]}, now=now,
                )
        elif routine == "pattern_mining":
            next_at = iso(now_dt + timedelta(hours=24))
            try:
                from .patterns import run_mining

                outcome = await run_mining(self.runtime, now_dt)
                log.info("pattern mining: %s", outcome)
            except Exception as exc:
                log.warning("pattern mining failed: %s", exc)
            try:
                from .render.cards import design_card_kinds

                designed = await design_card_kinds(self.runtime, now_dt)
                log.info("card kinds: %s", designed)
            except Exception as exc:
                log.warning("card design failed: %s", exc)
        self.store.finish_followup_check(item["id"], success=True, next_check_at=next_at, now=iso(self.clock()))

    # -- sources, signals, jobs ---------------------------------------------------------------------
    async def _sync_sources(self, now_dt: datetime) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for source_id, adapter in list(self.runtime.sources.items()):
            if not adapter.due(now_dt):
                continue
            lock = self._sync_locks.setdefault(source_id, asyncio.Lock())
            if lock.locked():
                continue
            async with lock:
                try:
                    result = await self.runtime.sync_source(adapter, now_dt)
                    summary[source_id] = {"added": result.added, "seen": result.seen, "ignored": result.ignored, "error": result.error}
                except Exception as exc:
                    summary[source_id] = {"error": str(exc)[:200]}
                    log.warning("source sync failed for %s: %s", source_id, exc)
        return summary

    async def _process_signals(self, now_dt: datetime) -> list[dict[str, Any]]:
        from .pipeline.process import process_bundle

        limit = int(self.config.get("limits", {}).get("max_bundles_per_tick", 5))
        bundles = self.store.claim_signal_bundles(iso(now_dt), iso(now_dt + timedelta(minutes=10)), limit)
        outcomes: list[dict[str, Any]] = []
        for bundle in bundles:
            try:
                outcomes.append(await process_bundle(self.runtime, bundle, self.clock()))
            except Exception as exc:
                log.exception("bundle %s failed", bundle.get("group_key"))
                try:
                    self.store.defer_signals(bundle["signals"], next_attempt_at=iso(now_dt + timedelta(minutes=15)), error=f"{type(exc).__name__}: {str(exc)[:150]}", now=iso(self.clock()))
                except Exception:
                    pass
                outcomes.append({"group": bundle.get("group_key"), "error": str(exc)[:100]})
        return outcomes

    def _retry_jobs(self, now_dt: datetime) -> int:
        from .pipeline.process import retry_pending_jobs

        try:
            return retry_pending_jobs(self.runtime, now_dt)
        except Exception as exc:
            log.warning("radar job retry failed: %s", exc)
            return 0

    async def _run_jobs(self, now_dt: datetime) -> dict[str, int]:
        now = iso(now_dt)
        recovered = self.store.recover_stale_jobs(now)
        started = 0
        max_concurrent = int(self.config.get("jobs", {}).get("max_concurrent", 2))
        free = max(0, max_concurrent - len(self.runtime.job_tasks))
        if free:
            jobs_cfg = self.config.get("jobs", {})
            longest = max(int(jobs_cfg.get("max_seconds", 300)), int(jobs_cfg.get("investigate_seconds", 900)))
            lease = iso(now_dt + timedelta(seconds=longest + 300))  # covers an investigation plus a clone
            for job in self.store.claim_queued_jobs(now, lease, free):
                if self.runtime.spawn_job(job) is not None:
                    started += 1
        return {"started": started, "running": len(self.runtime.job_tasks), "recovered": recovered}

    # -- legacy operations ----------------------------------------------------------------------------
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
                self.store.finish_followup_check(
                    followup_id, success=True, next_check_at=next_at, now=iso(self.clock())
                )
            except Exception:
                if not settled:
                    self.store.settle_budget(
                        bucket_start=day, resource_key=operation_id, units=operation.budget_units,
                        consumed=False, now=iso(self.clock()),
                    )
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
                "text": f"Radar: {parameters.get('url')} → {status_text}.",
                "run_id": run_id,
                "operation_id": operation_id,
                "urgent": True,
            },
            now=iso(now_dt),
        )

    # -- delivery -------------------------------------------------------------------------------------
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
        if start == end:
            return False  # no quiet window configured
        return start <= minute < end if start < end else minute >= start or minute < end

    async def _deliver_outbox(self, now_dt: datetime) -> int:
        if self.config["mode"] != "active" or not self.config["notifications"]["enabled"]:
            return 0
        quiet = self._quiet(now_dt)
        now = iso(now_dt)
        rows = self.store.claim_outbox(now, iso(now_dt + timedelta(minutes=2)), limit=10)
        delivered = 0
        for row in rows:
            payload = row["payload"]
            if quiet and not payload.get("urgent"):
                self.store.finish_outbox(
                    row["id"], fencing_token=row["fencing_token"], success=False, platform_message_id=None,
                    error="quiet_hours", next_attempt_at=iso(now_dt + timedelta(minutes=15)), now=iso(self.clock()),
                )
                continue
            try:
                destination = self.store.destination(row["destination_id"])
                receipt = await self.transports.send(row["platform"], destination, payload)
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

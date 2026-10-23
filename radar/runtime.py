"""Per-process runtime: store, config, LLM gate, sources, transports, consumer, jobs and the scheduler."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import load_config
from .ingress.webhook_consumer import WebhookConsumer
from .jobs.runner import JobRunner
from .jobs.service import record_job_outcome
from .llm import LlmGate
from .models import canonical_hash
from .pipeline.recall import Recall
from .providers.registry import build_calendar_provider, build_mail_provider
from .providers.cron_host import HostCronProvider
from .providers.repo_gh import GhRepoProvider
from .scheduler import Scheduler
from .sources.base import SourceAdapter, SyncResult
from .sources.calendar import CalendarSource
from .sources.cron_output import SOURCE_ID as CRON_SOURCE_ID, CronOutputSource
from .sources.github import GithubSource
from .sources.gmail import GmailSource
from .sources.session import SOURCE_ID as SESSION_SOURCE_ID, SessionDbPoller
from .transports import TransportRegistry

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def job_budget(config: dict[str, Any], job: dict[str, Any], task: dict[str, Any]) -> tuple[int, int]:
    """Turns and seconds for one job: an investigation or a fix on a work item gets the larger budget
    (``jobs.investigate_turns`` / ``jobs.investigate_seconds``), everything else the plain one."""
    jobs_cfg = config.get("jobs") or {}
    turns, seconds = int(jobs_cfg.get("max_turns", 20)), int(jobs_cfg.get("max_seconds", 300))
    if job.get("kind") != "native" and job.get("action_class") in ("read", "local") and task.get("kind") == "work_item":
        turns = max(turns, int(jobs_cfg.get("investigate_turns", 60)))
        seconds = max(seconds, int(jobs_cfg.get("investigate_seconds", 900)))
    return turns, seconds


class Runtime:
    def __init__(self, ctx: Any, store: Any, config: dict[str, Any], *, worker_id: str, clock: Callable[[], datetime] = utc_now,
                 transports: TransportRegistry | None = None, hermes_home: str | None = None):
        self.ctx = ctx
        self.store = store
        self.config = config
        self.clock = clock
        self.worker_id = worker_id
        self.hermes_home = hermes_home
        self.transports = transports or TransportRegistry()
        self.gate = LlmGate(ctx, store, config, clock=clock)
        self.recall = Recall(ctx, config, hermes_home=hermes_home)
        self.sources: dict[str, SourceAdapter] = {}
        self.paused = False
        self.task: asyncio.Task | None = None
        self.receiver: Any = None
        self._receiver_starting = False
        self.job_tasks: set[asyncio.Task] = set()
        self.push_events = 0
        self._config_hash = canonical_hash(config)
        jobs_cfg = config.get("jobs") or {}
        self.runner = JobRunner(
            max_turns=int(jobs_cfg.get("max_turns", 20)), max_seconds=int(jobs_cfg.get("max_seconds", 300)),
            model=jobs_cfg.get("model") or None,
        ) if jobs_cfg.get("enabled", True) else None
        self.consumer = WebhookConsumer(self.routes(), on_gmail_push=self.on_gmail_push, on_github=self.on_github)
        config["_hermes_home"] = hermes_home
        self.scheduler = Scheduler(store, config, worker_id=worker_id, transports=self.transports, clock=clock, runtime=self)

    # -- configuration ------------------------------------------------------------------------
    def refresh_config(self) -> bool:
        """Re-read the plugin settings; True when something changed. Sources are re-materialised
        when their list changed, so edits to config.yaml apply without a gateway restart."""
        try:
            fresh = load_config(self.ctx)
        except Exception as exc:
            log.warning("radar configuration not reloaded: %s", exc)
            return False
        digest = canonical_hash(fresh)
        if digest == self._config_hash:
            return False
        sources_changed = canonical_hash(fresh.get("sources")) != canonical_hash(self.config.get("sources"))
        self.config.clear()
        self.config.update(fresh)
        self._config_hash = digest
        self.consumer.routes = self.routes()
        self.ensure_receiver()
        if sources_changed:
            try:
                from .scheduler import sync_configured_sources

                sync_configured_sources(self.store, self.config, self.clock())
                self.reload_sources()
            except Exception as exc:
                log.warning("radar sources not reloaded: %s", exc)
        log.info("radar configuration reloaded%s", " (sources changed)" if sources_changed else "")
        return True

    def routes(self) -> dict[str, dict[str, Any]]:
        receiver = (self.config.get("ingress") or {}).get("gmail_receiver") or {}
        route = str(receiver.get("route") or "gmail-push")
        routes: dict[str, dict[str, Any]] = {route: {"kind": "gmail_push", "skip": True}}
        for source in self.config.get("sources") or []:
            if isinstance(source, dict) and source.get("kind") == "github" and source.get("route") and source.get("enabled", True):
                routes[str(source["route"])] = {"kind": "github", "skip": bool(source.get("consume", False))}
        return routes

    def reload_sources(self) -> None:
        call_mcp = getattr(self.ctx, "call_mcp", None)
        preferences = self.store.list_preferences(active_only=True)
        adapters: dict[str, SourceAdapter] = {}
        for row in self.store.list_sources(enabled_only=True):
            kind = row.get("kind")
            try:
                if kind == "gmail" and callable(call_mcp):
                    provider = build_mail_provider(call_mcp, {**(row.get("config") or {}), "id": row["id"]})
                    adapters[row["id"]] = GmailSource(row, self.config, self.store, provider=provider, preferences=preferences, clock=self.clock)
                elif kind == "calendar" and callable(call_mcp):
                    provider = build_calendar_provider(call_mcp, {**(row.get("config") or {}), "id": row["id"]}, timezone=str(self.config.get("owner", {}).get("timezone") or ""))
                    adapters[row["id"]] = CalendarSource(row, self.config, self.store, provider=provider, preferences=preferences, clock=self.clock)
                elif kind == "github":
                    gh = GhRepoProvider()
                    adapters[row["id"]] = GithubSource(row, self.config, self.store, provider=gh if gh.available else None, preferences=preferences, clock=self.clock)
            except Exception as exc:
                log.warning("source %s not loaded: %s", row.get("id"), exc)
        observation = self.config.get("observation") or {}
        if int(observation.get("state_db_poll_seconds") or 0) > 0 and observation.get("local_sessions"):
            try:
                row = self.store.get_source(SESSION_SOURCE_ID)
            except KeyError:
                now = iso(self.clock())
                self.store.ensure_local_observation_source(now, int(self.config.get("retention", {}).get("observations_days", 30)))
                row = self.store.get_source(SESSION_SOURCE_ID)
            adapters[SESSION_SOURCE_ID] = SessionDbPoller(row, self.config, self.store, clock=self.clock)
        if self.store.list_patterns(statuses=("automated",), limit=1):
            try:
                try:
                    row = self.store.get_source(CRON_SOURCE_ID)
                except KeyError:
                    now = iso(self.clock())
                    self.store.add_source(source_id=CRON_SOURCE_ID, kind="hermes", account_ref="cron", resource_scope={},
                                          retention_days=int(self.config.get("retention", {}).get("observations_days", 30)), now=now)
                    row = self.store.get_source(CRON_SOURCE_ID)
                adapters[CRON_SOURCE_ID] = CronOutputSource(row, self.config, self.store, output_dir=lambda: HostCronProvider().output_dir(), clock=self.clock)
            except Exception as exc:
                log.warning("cron output source not loaded: %s", exc)
        self.sources = adapters
        self.warm_recall()

    def warm_recall(self) -> None:
        """Pre-load the memory provider in the background when recall is enabled; harmless without a loop."""
        if not (self.config.get("context") or {}).get("memory", True) or self.recall.memory.provider is not None:
            return
        spawn = getattr(self.ctx, "spawn_task", None)
        if not callable(spawn):
            return
        coroutine = self.recall.memory.warm()
        try:
            spawn(coroutine, name="radar:recall-warm")
        except Exception:  # CLI without a loop
            coroutine.close()

    def refresh_preferences(self) -> list[dict[str, Any]]:
        """Hand the active preferences to the live source adapters (triage applies them at ingest)."""
        preferences = self.store.list_preferences(active_only=True)
        for adapter in self.sources.values():
            if hasattr(adapter, "preferences"):
                adapter.preferences = preferences
        return preferences

    # -- events ---------------------------------------------------------------------------------
    def on_github(self, route: str, payload: dict[str, Any], delivery_id: str) -> None:
        """A GitHub delivery on a host webhook route: every github source bound to that route sees it."""
        now_dt = self.clock()
        for adapter in self.sources.values():
            if isinstance(adapter, GithubSource) and adapter.route == route:
                try:
                    outcome = adapter.handle_event(payload, delivery_id, now_dt)
                    log.info("github %s on %s: %s", delivery_id or "event", route, outcome)
                except Exception as exc:
                    log.warning("github event on %s not ingested: %s", route, exc)

    def write_provider(self, kind: str, *, account: str = "") -> Any:
        """A write-enabled provider for a native operation, or None when none is configured."""
        call_mcp = getattr(self.ctx, "call_mcp", None)
        if kind == "repo":
            gh = GhRepoProvider(allow_write=True)
            return gh if gh.available else None
        if kind == "cron":
            return HostCronProvider(allow_write=True)
        if kind == "store":
            return self.store
        if not callable(call_mcp):
            return None
        for source in self.config.get("sources") or []:
            if not isinstance(source, dict) or not source.get("enabled", True):
                continue
            if kind == "mail" and source.get("kind") == "gmail" and (not account or str(source.get("address") or "").lower() == account.lower()):
                return build_mail_provider(call_mcp, source, allow_write=True)
            if kind == "calendar" and source.get("kind") == "calendar" and (not account or account in (str(source.get("account") or ""), str(source.get("calendar_id") or ""), str(source.get("id") or ""))):
                return build_calendar_provider(call_mcp, source, allow_write=True, timezone=str(self.config.get("owner", {}).get("timezone") or ""))
        return None

    def on_gmail_push(self, push: dict[str, str]) -> None:
        self.push_events += 1
        address = push.get("address", "")
        for adapter in self.sources.values():
            if isinstance(adapter, GmailSource) and adapter.address.lower() == address:
                adapter.handle_push(push)
                return
        log.info("gmail push for %s matched no configured source", address)

    def repo_provider(self) -> Any:
        """A read-only ``gh`` provider when the CLI is installed, else None."""
        gh = GhRepoProvider()
        return gh if gh.available else None

    async def refresh_artifacts(self, now_dt: datetime) -> dict[str, int]:
        """Re-check the pull requests behind the next summary's items, in a worker thread, under the profile's secrets."""
        from .hostscope import profile_secret_scope
        from .pipeline.refresh import refresh_before_digest

        provider = self.repo_provider()
        if provider is None:
            return {"checked": 0, "changed": 0}
        with profile_secret_scope(self.hermes_home):
            return await asyncio.to_thread(refresh_before_digest, self.store, self.config, now_dt, provider=provider)

    async def sync_source(self, adapter: SourceAdapter, now_dt: datetime) -> SyncResult:
        from .hostscope import profile_secret_scope

        with profile_secret_scope(self.hermes_home):
            result = await asyncio.to_thread(adapter.sync, now_dt)
        now = iso(self.clock())
        try:
            self.store.update_source_cursor(adapter.id, result.cursor, now, success=result.error is None)
            if result.health:
                self.store.update_source_health(adapter.id, result.health, now)
            adapter.row = self.store.get_source(adapter.id)
        except Exception as exc:
            log.warning("source %s cursor not saved: %s", adapter.id, exc)
        return result

    # -- jobs -----------------------------------------------------------------------------------
    def spawn_job(self, job: dict[str, Any]) -> asyncio.Task | None:
        if self.runner is None and job.get("kind") != "native":
            return None
        task = asyncio.create_task(self._run_job(job), name=f"radar:job:{job['id']}")
        self.job_tasks.add(task)
        task.add_done_callback(self.job_tasks.discard)
        return task

    async def _run_job(self, job: dict[str, Any]) -> None:
        now = iso(self.clock())

        def on_pid(pid: int) -> None:
            try:
                self.store.set_job_pid(job["id"], pid, now)
            except Exception:
                pass

        try:
            if job.get("kind") == "native":
                from .operations import native

                outcome = await native.run_job(self, job)
            elif self.runner is None:
                from .jobs.runner import JobOutcome

                outcome = JobOutcome("failed", error="delegated jobs are disabled")
            else:
                allow = ""
                if job.get("action_class") == "external" and job.get("proposal_id"):
                    from .jobs.firewall import allowed_tools_for

                    try:
                        allow = allowed_tools_for((self.store.get_proposal(job["proposal_id"]) or {}).get("operation_id"))
                    except Exception:
                        allow = ""
                task = {}
                try:
                    task = self.store.get_task(job["task_id"]) if job.get("task_id") else {}
                except Exception:
                    task = {}
                turns, seconds = job_budget(self.config, job, task)
                await self.ensure_checkout(job, task)
                outcome = await self.runner.run(job, on_pid=on_pid, allow=allow, turns=turns, seconds=seconds)
        except asyncio.CancelledError:
            from .jobs.runner import JobOutcome

            outcome = JobOutcome("cancelled", error="runtime stopped")
        except Exception as exc:
            from .jobs.runner import JobOutcome

            outcome = JobOutcome("failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
        try:
            record_job_outcome(self.store, self.config, job=job, outcome=outcome, now_dt=self.clock())
        except Exception as exc:
            log.warning("job %s outcome not recorded: %s", job["id"], exc)

    async def ensure_checkout(self, job: dict[str, Any], task: dict[str, Any]) -> None:
        """A work item on a repository the owner has no checkout for gets one: a partial clone under radar's own
        workspaces directory, reused by later steps. Reversible and private, so it needs no permission."""
        if job.get("workdir") or job.get("kind") == "native":
            return
        jobs_cfg = self.config.get("jobs") or {}
        if not jobs_cfg.get("auto_clone", True):
            return
        repo = str((task.get("entities") or {}).get("repo") or "").strip()
        if not REPO_RE.match(repo):
            return
        root = Path(str(self.config.get("_hermes_home") or "")) / "radar" / "workspaces" if self.config.get("_hermes_home") else None
        if root is None:
            return
        path = root / repo.lower()
        if not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                process = await asyncio.create_subprocess_exec(
                    "gh", "repo", "clone", repo, str(path), "--", "--filter=blob:none",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=float(jobs_cfg.get("clone_seconds", 240)))
            except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
                log.info("radar could not clone %s: %s", repo, exc)
                return
            if process.returncode != 0 or not (path / ".git").exists():
                log.info("radar could not clone %s: %s", repo, (stderr or b"").decode("utf-8", "replace")[-200:])
                return
            log.info("radar cloned %s for task %s", repo, task.get("id"))
        job["workdir"] = str(path)
        job["worktree"] = True
        try:
            self.store.set_job_workdir(job["id"], str(path), iso(self.clock()))
        except Exception:
            pass

    # -- receiver -----------------------------------------------------------------------------
    def ensure_receiver(self) -> None:
        """Start or stop the Gmail push receiver to match ``ingress.gmail_receiver.enabled`` without a restart."""
        wanted = bool(((self.config.get("ingress") or {}).get("gmail_receiver") or {}).get("enabled"))
        spawn = getattr(self.ctx, "spawn_task", None)
        if not callable(spawn):
            return
        if wanted and self.receiver is None and not self._receiver_starting:
            self._receiver_starting = True

            async def _start() -> None:
                try:
                    if not await self.start_receiver():
                        log.info("radar gmail push receiver stays off (disabled or secrets missing)")
                except Exception as exc:
                    log.warning("radar gmail push receiver failed to start: %s", exc)
                finally:
                    self._receiver_starting = False

            coroutine = _start()
            try:
                spawn(coroutine, name="radar:gmail-receiver")
            except Exception as exc:  # no event loop here (CLI): the gateway starts it
                coroutine.close()
                self._receiver_starting = False
                log.debug("receiver not started here: %s", exc)
        elif not wanted and self.receiver is not None:
            receiver, self.receiver = self.receiver, None

            async def _stop() -> None:
                try:
                    await receiver.stop()
                except Exception as exc:
                    log.debug("receiver stop failed: %s", exc)

            coroutine = _stop()
            try:
                spawn(coroutine, name="radar:gmail-receiver-stop")
            except Exception:
                coroutine.close()

    async def start_receiver(self) -> bool:
        receiver_cfg = (self.config.get("ingress") or {}).get("gmail_receiver") or {}
        if not receiver_cfg.get("enabled"):
            return False
        token = os.environ.get("RADAR_GMAIL_PUSH_TOKEN", "")
        secret = os.environ.get("RADAR_GMAIL_PUSH_HERMES_SECRET", "")
        if not token or not secret:
            log.warning("gmail push receiver disabled: RADAR_GMAIL_PUSH_TOKEN / RADAR_GMAIL_PUSH_HERMES_SECRET missing")
            return False
        from .ingress.gmail_push_receiver import GmailPushReceiver

        accounts = {str(s.get("address", "")).lower() for s in self.config.get("sources", []) if s.get("kind") == "gmail" and s.get("address")}
        self.receiver = GmailPushReceiver(
            token=token, public_url=str(receiver_cfg.get("public_url") or ""),
            hermes_webhook_url=str(receiver_cfg.get("hermes_webhook_url") or "http://127.0.0.1:8644/webhooks/gmail-push"),
            hermes_secret=secret, route=str(receiver_cfg.get("route") or "gmail-push"),
            expected_subscription=str(receiver_cfg.get("expected_subscription") or os.environ.get("RADAR_GMAIL_PUSH_SUBSCRIPTION", "")),
            allowed_accounts=accounts, service_account_email=str(receiver_cfg.get("service_account_email") or os.environ.get("RADAR_GMAIL_PUSH_SA_EMAIL", "")),
            require_oidc=bool(receiver_cfg.get("require_oidc", True)),
        )
        await self.receiver.start(host=str(receiver_cfg.get("host") or "127.0.0.1"), port=int(receiver_cfg.get("port") or 8666))
        return True

    async def stop(self) -> None:
        try:
            self.recall.memory.close()
        except Exception:
            pass
        if self.task is not None and not self.task.done():
            self.task.cancel()
        for job_task in list(self.job_tasks):
            job_task.cancel()
        if self.receiver is not None:
            try:
                await self.receiver.stop()
            except Exception:
                pass
            self.receiver = None

    def status(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "scheduler_running": bool(self.task is not None and not self.task.done()),
            "sources": sorted(self.sources),
            "receiver": bool(self.receiver is not None),
            "jobs_running": len(self.job_tasks),
            "push_events": self.push_events,
            "llm": self.gate.available,
            "recall": self.recall_status(),
        }

    def recall_status(self) -> dict[str, Any]:
        memory = getattr(self.recall, "memory", None)
        provider = getattr(memory, "provider", None)
        tool = memory.tool() if memory is not None and memory.provider is not None else None
        name = getattr(provider, "name", None) if provider is not None else None
        if callable(name):
            try:
                name = name()
            except Exception:
                name = None
        return {
            "provider": (str(name) if name else type(provider).__name__) if provider is not None else None,
            "tool": tool[0] if tool else None, "calls": getattr(memory, "calls", 0), "last_error": getattr(memory, "last_error", ""),
            "sessions": self.recall.search_fn is not None,
        }

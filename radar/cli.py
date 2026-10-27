from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_config
from .scheduler import Scheduler, sync_configured_sources


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def setup_cli(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="radar_action")
    actions.add_parser("status", help="Show scoped configuration and storage status")
    actions.add_parser("validate", help="Validate configuration without opening the database")
    actions.add_parser("run-once", help="Synchronize configured sources and run one scheduler cycle")
    actions.add_parser("discover", help="Classify the profile's MCP servers by the tools they expose")
    setup = actions.add_parser("setup", help="Interactive onboarding: trust level, owner, sources, Gmail push")
    setup.add_argument("--yes", action="store_true", help="Accept defaults without prompting")
    sync = actions.add_parser("sync", help="Sync one configured source now")
    sync.add_argument("source_id")
    replay = actions.add_parser("replay", help="Re-queue one signal and process its bundle once")
    replay.add_argument("signal_id")
    tasks = actions.add_parser("tasks", help="List tracked tasks")
    tasks.add_argument("--all", action="store_true")
    task = actions.add_parser("task", help="Act on one task as the owner would from a notice: done, later, mute, unmute")
    task.add_argument("task_id")
    task.add_argument("task_action", choices=["done", "later", "mute", "unmute"])
    explain = actions.add_parser("explain", help="Explain one task; --html writes a standalone report")
    explain.add_argument("task_id")
    explain.add_argument("--html", action="store_true")
    gmail = actions.add_parser("gmail", help="Gmail push helpers")
    gmail_sub = gmail.add_subparsers(dest="gmail_action")
    watch = gmail_sub.add_parser("watch", help="Renew users.watch for push-enabled Gmail sources")
    watch.add_argument("--status", action="store_true")
    receiver = gmail_sub.add_parser("receiver", help="Run the push receiver standalone (foreground)")
    github = actions.add_parser("github", help="GitHub source helpers")
    github_sub = github.add_subparsers(dest="github_action")
    github_route = github_sub.add_parser("route", help="Install a radar-owned host webhook route for GitHub deliveries (no agent turn)")
    github_route.add_argument("name", nargs="?", default="github-radar")
    receiver.add_argument("--port", type=int)
    imp = actions.add_parser("import", help="Import data from another plugin")
    imp.add_argument("what", choices=["mail-intelligence"])
    imp.add_argument("--path", help="state.sqlite to import from")
    actions.add_parser("signals", help="Show recent signals and their triage")
    nxt = actions.add_parser("next", help="Launch the next notice now, ignoring quiet hours")
    nxt.add_argument("--dry-run", action="store_true", help="Only preview the next notice without sending it")


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str))


def command(ctx, args: argparse.Namespace) -> int:
    action = getattr(args, "radar_action", None)
    if not action:
        print("Usage: hermes radar {status|validate|run-once|discover|setup|sync|replay|tasks|task|explain|gmail|import|signals|next}")
        return 2
    try:
        if action == "discover":
            return _discover()
        if action == "setup":
            return _setup(ctx, yes=bool(getattr(args, "yes", False)))
        cfg = load_config(ctx)
        if action == "validate":
            _print({"valid": True, "enabled": cfg["enabled"], "mode": cfg["mode"], "trust": cfg["trust"]["level"], "sources": [s["id"] for s in cfg["sources"]]})
            return 0
        if not cfg["enabled"]:
            if action == "status":
                _print({"enabled": False, "configured": False, "mode": cfg["mode"], "store": None})
                return 0
            _print({"error": "plugin_disabled", "hint": "set plugins.entries.radar.settings.enabled: true"})
            return 1
        from . import _runtime, _store

        store = _store(ctx)
        if action == "status":
            _print({"store": store.status(), "pending_signals": store.pending_signal_count(),
                    "sources": [{"id": s["id"], "kind": s["kind"], "health": s.get("health"), "cursor": s.get("cursor")} for s in store.list_sources(enabled_only=False)]})
            return 0
        if action == "run-once":
            synced = sync_configured_sources(store, cfg)
            runtime = _runtime(ctx)
            runtime.reload_sources()
            result = asyncio.run(runtime.scheduler.tick())
            _print({"synced": synced, "cycle": result})
            return 0
        if action == "sync":
            sync_configured_sources(store, cfg)
            runtime = _runtime(ctx)
            runtime.reload_sources()
            adapter = runtime.sources.get(args.source_id) or runtime.sources.get(f"configured:{args.source_id}")
            if adapter is None:
                _print({"error": "unknown_source", "available": sorted(runtime.sources)})
                return 1
            adapter.push_requested = True
            result = asyncio.run(runtime.sync_source(adapter, datetime.now(timezone.utc)))
            _print({"added": result.added, "seen": result.seen, "ignored": result.ignored, "error": result.error, "cursor": result.cursor})
            return 0 if not result.error else 1
        if action == "replay":
            runtime = _runtime(ctx)
            now = datetime.now(timezone.utc)
            signal = store.get_signal(args.signal_id)
            with store.transaction() as db:
                db.execute(
                    "UPDATE signals SET triage_status='pending', next_attempt_at=?, lease_until=NULL, attempts=0 WHERE profile_key=? AND owner_id=? AND id=?",
                    (now.isoformat().replace("+00:00", "Z"), *store._scope_values, signal["id"]),
                )
            from .pipeline.process import process_bundle

            bundles = store.claim_signal_bundles(now.isoformat().replace("+00:00", "Z"), (now.replace(microsecond=0)).isoformat().replace("+00:00", "Z")[:-1] + "Z", 50)
            target = next((b for b in bundles if any(s["id"] == signal["id"] for s in b["signals"])), None)
            if target is None:
                _print({"error": "signal_not_claimable"})
                return 1
            _print(asyncio.run(process_bundle(runtime, target, now)))
            return 0
        if action == "tasks":
            statuses = None if getattr(args, "all", False) else ("open", "waiting", "needs_user", "in_progress", "snoozed")
            rows = []
            for task in store.list_tasks(statuses=statuses, limit=100):
                proposals = store.list_proposals(statuses=("pending", "sent", "snoozed", "waiting", "executing"), task_id=task["id"], limit=10)
                rows.append({"id": task["id"], "kind": task["kind"], "status": task["status"], "title": task["title"],
                             "due_at": task.get("due_at"), "next_review_at": task.get("next_review_at"),
                             "missing": [c.get("item") for c in task.get("checklist") or [] if isinstance(c, dict) and c.get("status") == "missing"],
                             "proposals": [{"id": p["id"], "title": p["title"], "status": p["status"], "class": p["action_class"]} for p in proposals]})
            _print(rows)
            return 0
        if action == "task":
            from .delivery.responses import act_on_task

            full_id = next((t["id"] for t in store.list_tasks(limit=500) if t["id"] == args.task_id or t["id"].startswith(args.task_id)), None)
            if full_id is None:
                _print({"error": "unknown_task"})
                return 1
            _print(act_on_task(store, cfg, task_id=full_id, action=args.task_action, actor="cli", now_dt=datetime.now(timezone.utc)))
            return 0
        if action == "explain":
            from . import explain_tool

            print(explain_tool(ctx, {"task_id": args.task_id, "html": bool(getattr(args, "html", False))}))
            return 0
        if action == "signals":
            _print([{k: v for k, v in s.items() if k not in {"raw_content", "meta", "extract"}} for s in store.list_signals(limit=30)])
            return 0
        if action == "next":
            from .delivery.preview import deliver_next, preview_next

            now_dt = datetime.now(timezone.utc)
            if getattr(args, "dry_run", False):
                _print(preview_next(store, cfg, now_dt))
            else:
                _print(deliver_next(store, cfg, now_dt))
            return 0
        if action == "github":
            if getattr(args, "github_action", None) == "route":
                _install_github_route(_hermes_home(), args.name)
                return 0
            print("Usage: hermes radar github route [name]")
            return 1
        if action == "gmail":
            return _gmail(ctx, cfg, args)
        if action == "import":
            return _import(ctx, cfg, args)
        print(f"Unknown radar action: {action}")
        return 2
    except Exception as exc:
        _print({"error": str(exc)})
        return 1


# -- discovery and setup --------------------------------------------------------------------------
def _discover() -> int:
    from .providers.discovery import discover

    rows = discover(_hermes_home())
    for row in rows:
        mark = row["provider"] or ("unknown" if row["cached"] else "not cached")
        print(f"{row['server']:<28} {row['kind'] or '-':<10} {mark:<12} {row['package']}")
    if not rows:
        print("No mcp_servers configured in this profile.")
    return 0


def _ask(prompt: str, default: str = "", *, yes: bool = False) -> str:
    if yes:
        return default
    try:
        answer = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    except EOFError:
        return default
    return answer or default


def _setup(ctx, *, yes: bool) -> int:
    from .providers.discovery import discover, suggest_sources
    from .trust import describe

    home = _hermes_home()
    cfg = load_config(ctx)
    from .trust import guide_text, level_summary

    print("Radar setup\n")
    language = _ask("Language (es | en)", cfg["owner"].get("language") or "es", yes=yes)
    print("\n" + guide_text(language) + "\n")
    level = _ask("Trust level (cautious | balanced | autonomous)", cfg["trust"]["level"], yes=yes)
    if level not in ("cautious", "balanced", "autonomous"):
        level = "balanced"
    print(f"  {level_summary(level, language)}")
    print(f"  ({', '.join(describe(level)['auto_classes'])} run alone; external asks)")
    owner_id = _ask("Owner id (any stable label)", cfg["owner"].get("id") or "owner", yes=yes)
    timezone_name = _ask("Timezone", cfg["owner"].get("timezone") or "UTC", yes=yes)
    quiet_start = _ask("Quiet hours start (HH:MM)", cfg["notifications"]["quiet_start"], yes=yes)
    quiet_end = _ask("Quiet hours end (HH:MM)", cfg["notifications"]["quiet_end"], yes=yes)
    chat_id = _ask("Telegram chat id for notices (empty to skip)", str((cfg["destination"].get("address") or {}).get("chat_id") or ""), yes=yes)
    owner_platform_id = _ask("Telegram user id of the owner", str(cfg["destination"].get("owner_platform_id") or ""), yes=yes) if chat_id else ""

    rows = discover(home)
    existing = {s["id"]: s for s in cfg["sources"]}
    chosen: list[dict[str, Any]] = [s for s in cfg["sources"] if s.get("kind") == "http_status"]
    mcp_servers: list[str] = []
    print("\nMCP servers:")
    for row in rows:
        print(f"  {row['server']:<28} {row['kind'] or '-':<10} {row['provider'] or ('unknown' if row['cached'] else 'not cached')}")
    for suggestion in suggest_sources(rows):
        current = existing.get(suggestion["id"], {})
        merged = {**suggestion, **{k: v for k, v in current.items() if k in suggestion}}
        if suggestion["kind"] == "gmail":
            use = _ask(f"Watch Gmail through {suggestion['server']}? (y/n)", "y" if current or yes else "n", yes=yes)
            if use.lower() != "y":
                continue
            merged["address"] = _ask(f"  email address of {suggestion['server']}", current.get("address", ""), yes=yes)
            if "@" not in merged["address"]:
                print("  skipped: an address is required")
                continue
            push = dict(current.get("push") or merged.get("push") or {})
            want_push = _ask("  enable Gmail push (Pub/Sub)? (y/n)", "y" if push.get("enabled") else "n", yes=yes)
            if want_push.lower() == "y":
                push["enabled"] = True
                push["topic"] = _ask("  Pub/Sub topic (projects/<id>/topics/<name>)", push.get("topic", ""), yes=yes)
                default_creds = push.get("credentials_path") or _guess_credentials_dir(home, suggestion["server"])
                push["credentials_path"] = _ask("  credentials directory of that MCP (credentials.json + gcp-oauth.keys.json)", default_creds, yes=yes)
                if not push["topic"] or not push["credentials_path"]:
                    push["enabled"] = False
            else:
                push["enabled"] = False
            merged["push"] = push
        elif suggestion["kind"] == "calendar":
            use = _ask(f"Watch calendar through {suggestion['server']}? (y/n)", "y" if current else "n", yes=yes)
            if use.lower() != "y":
                continue
        chosen.append(merged)
        mcp_servers.append(suggestion["server"])

    settings = {
        "enabled": True, "mode": "active" if chat_id else cfg["mode"],
        "owner": {"id": owner_id, "timezone": timezone_name, "language": language},
        "trust": {**cfg["trust"], "level": level},
        "trust_level": level,
        "notifications": {**cfg["notifications"], "enabled": bool(chat_id), "quiet_start": quiet_start, "quiet_end": quiet_end},
        "observation": {**cfg["observation"], "local_sessions": True},
        "sources": chosen,
    }
    if chat_id:
        settings["destination"] = {**cfg["destination"], "platform": "telegram", "gateway_connection_ref": "telegram:default",
                                   "address": {"chat_id": str(chat_id)}, "owner_platform_id": str(owner_platform_id)}
    any_push = any(s.get("kind") == "gmail" and (s.get("push") or {}).get("enabled") for s in chosen)
    if any_push:
        public_url = _ask("Public HTTPS URL that reaches the receiver (tunnel)", cfg["ingress"]["gmail_receiver"].get("public_url", ""), yes=yes)
        settings["ingress"] = {"gmail_receiver": {**cfg["ingress"]["gmail_receiver"], "enabled": True, "public_url": public_url}}
    from .schemas import validate_config
    from .config import _merge
    from .schemas import CONFIG_DEFAULTS

    merged_cfg = _merge(CONFIG_DEFAULTS, {**cfg, **settings})
    ok, errors = validate_config(merged_cfg)
    if not ok:
        _print({"error": "invalid settings", "details": errors})
        return 1
    setter = getattr(ctx, "set_config", None)
    if callable(setter):
        for key, value in settings.items():
            setter(key, value)
        print("\nSettings written to plugins.entries.radar.settings.")
    else:
        print("\nSettings (copy into plugins.entries.radar.settings):")
        _print(settings)
    _grant_mcp_allowlist(mcp_servers)
    if any_push:
        _install_push_route(home, cfg["ingress"]["gmail_receiver"].get("route", "gmail-push"))
    print("\nNext: enable the plugin (plugins.enabled: [radar]), restart the gateway, then send /radar verify from the owner's chat.")
    return 0


def _guess_credentials_dir(home: Path, server: str) -> str:
    try:
        import yaml

        config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        env = ((config.get("mcp_servers") or {}).get(server) or {}).get("env") or {}
        for key in ("GMAIL_CREDENTIALS_PATH", "GMAIL_OAUTH_PATH", "GMAIL_CONFIG_DIR"):
            value = env.get(key)
            if isinstance(value, str) and value:
                path = Path(value).expanduser()
                return str(path if path.is_dir() else path.parent)
    except Exception:
        pass
    return ""


def _grant_mcp_allowlist(servers: list[str]) -> None:
    if not servers:
        return
    try:
        from hermes_cli.config import load_config as host_load, save_config as host_save
    except ImportError:
        print(f"Add to config.yaml: plugins.entries.radar.mcp_allowlist: {servers}")
        return
    try:
        config = host_load() or {}
        entry = config.setdefault("plugins", {}).setdefault("entries", {}).setdefault("radar", {})
        current = list(entry.get("mcp_allowlist") or [])
        for server in servers:
            if server not in current:
                current.append(server)
        entry["mcp_allowlist"] = current
        host_save(config, merge_existing=True)
        print(f"mcp_allowlist granted: {current}")
    except Exception as exc:
        print(f"Could not write mcp_allowlist ({exc}). Add plugins.entries.radar.mcp_allowlist: {servers} manually.")


def _install_github_route(home: Path, route: str) -> None:
    """A host webhook route radar consumes: GitHub signs deliveries with the route secret; no prompt, no agent turn."""
    path = home / "webhook_subscriptions.json"
    try:
        routes = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        routes = {}
    existing = routes.get(route) or {}
    routes[route] = {
        **{k: v for k, v in existing.items() if k in {"created_at", "profile"}},
        "description": "Radar: GitHub events (consumed by the radar plugin, no agent turn)",
        "events": [], "secret": existing.get("secret") or secrets.token_hex(32), "prompt": "", "skills": [], "deliver": "log", "enabled": True,
        "created_at": existing.get("created_at") or datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(routes, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"Webhook route '{route}' installed (deliver: log, no prompt). On GitHub: Settings → Webhooks → payload URL "
          f"<public webhook base>/webhooks/{route}, content type application/json, secret = the route's secret in "
          f"{path} (never paste it in chat). Then add to the radar source: route: {route}, consume: true.")


def _install_push_route(home: Path, route: str) -> None:
    """Own the webhook route: no prompt, no agent turn; secrets go to .env, not config."""
    path = home / "webhook_subscriptions.json"
    try:
        routes = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        routes = {}
    env_path = home / ".env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    secret = _env_value(env_text, "RADAR_GMAIL_PUSH_HERMES_SECRET") or (routes.get(route) or {}).get("secret") or secrets.token_hex(32)
    token = _env_value(env_text, "RADAR_GMAIL_PUSH_TOKEN") or secrets.token_urlsafe(24)
    routes[route] = {
        **{k: v for k, v in (routes.get(route) or {}).items() if k in {"created_at", "profile"}},
        "description": "Radar: Gmail push (consumed by the radar plugin, no agent turn)",
        "events": [], "secret": secret, "prompt": "", "skills": [], "deliver": "log", "enabled": True,
        "created_at": (routes.get(route) or {}).get("created_at") or datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(routes, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    additions = []
    if not _env_value(env_text, "RADAR_GMAIL_PUSH_HERMES_SECRET"):
        additions.append(f"RADAR_GMAIL_PUSH_HERMES_SECRET={secret}")
    if not _env_value(env_text, "RADAR_GMAIL_PUSH_TOKEN"):
        additions.append(f"RADAR_GMAIL_PUSH_TOKEN={token}")
    if additions:
        with env_path.open("a", encoding="utf-8") as handle:
            handle.write(("\n" if env_text and not env_text.endswith("\n") else "") + "\n".join(additions) + "\n")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
    print(f"Webhook route '{route}' installed (deliver: log, no prompt). Pub/Sub push endpoint: <public_url>?token=<RADAR_GMAIL_PUSH_TOKEN> (OIDC required).")


def _env_value(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# -- gmail helpers ----------------------------------------------------------------------------------
def _gmail(ctx, cfg: dict[str, Any], args: argparse.Namespace) -> int:
    sub = getattr(args, "gmail_action", None)
    if sub == "watch":
        from .ingress.gmail_watch import profile, renew_watch

        results = {}
        for source in cfg["sources"]:
            push = source.get("push") or {}
            if source.get("kind") != "gmail" or not push.get("enabled"):
                continue
            try:
                if getattr(args, "status", False):
                    results[source["id"]] = profile(push["credentials_path"])
                else:
                    results[source["id"]] = renew_watch(push["credentials_path"], push["topic"], source.get("labels") or ["INBOX"])
            except Exception as exc:
                results[source["id"]] = {"error": str(exc)[:300]}
        _print(results or {"info": "no push-enabled gmail sources"})
        return 0
    if sub == "receiver":
        from . import _runtime

        runtime = _runtime(ctx)
        receiver_cfg = cfg["ingress"]["gmail_receiver"]
        if getattr(args, "port", None):
            receiver_cfg["port"] = args.port
        receiver_cfg["enabled"] = True

        async def run() -> None:
            started = await runtime.start_receiver()
            if not started:
                print("receiver not started: check RADAR_GMAIL_PUSH_TOKEN and RADAR_GMAIL_PUSH_HERMES_SECRET in .env")
                return
            print(f"receiver listening on {receiver_cfg['host']}:{receiver_cfg['port']} (Ctrl+C to stop)")
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                await runtime.receiver.stop()

        asyncio.run(run())
        return 0
    print("Usage: hermes radar gmail {watch [--status]|receiver [--port N]}")
    return 2


# -- import -----------------------------------------------------------------------------------------
def _import(ctx, cfg: dict[str, Any], args: argparse.Namespace) -> int:
    from . import _store
    from .preferences import validate_preference

    path = Path(getattr(args, "path", None) or (_hermes_home() / "mail-intelligence" / "state.sqlite"))
    if not path.exists():
        _print({"error": f"not found: {path}"})
        return 1
    store = _store(ctx)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    imported = 0
    skipped = 0
    try:
        rows = db.execute(
            """SELECT p.payload FROM preferences p WHERE p.version=(
                 SELECT MAX(q.version) FROM preferences q WHERE q.profile=p.profile AND q.owner=p.owner AND q.id=p.id)"""
        ).fetchall()
    except sqlite3.Error as exc:
        _print({"error": f"cannot read preferences: {exc}"})
        return 1
    existing = {p["id"] for p in store.list_preferences()}
    for (payload,) in rows:
        try:
            data = json.loads(payload)
        except ValueError:
            skipped += 1
            continue
        if data.get("status") != "active" or data["id"] in existing:
            skipped += 1
            continue
        record = {
            "id": data["id"], "dimension": data.get("dimension", "notification"), "scope": data.get("scope") or {},
            "effect": data.get("effect", "silent"), "exceptions": data.get("exceptions") or [],
            "instruction": data.get("instruction") or data["id"], "origin": "imported", "status": "active",
        }
        try:
            validate_preference(record)
            store.save_preference(record, expected_version=0, now=now, feedback_ref=f"import:{path.name}")
            imported += 1
        except ValueError:
            skipped += 1
    db.close()
    _print({"imported": imported, "skipped": skipped, "source": str(path)})
    return 0

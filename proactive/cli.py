from __future__ import annotations

import argparse
import asyncio
import json

from .config import load_config
from .scheduler import Scheduler, sync_configured_sources


def setup_cli(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="proactive_action")
    actions.add_parser("status", help="Show scoped configuration and storage status")
    actions.add_parser("validate", help="Validate configuration without opening the database")
    actions.add_parser("run-once", help="Synchronize configured sources and run one scheduler cycle")


def command(ctx, args: argparse.Namespace) -> int:
    action = getattr(args, "proactive_action", None)
    if not action:
        print("Usage: hermes proactive {status|validate|run-once}")
        return 2
    try:
        cfg = load_config(ctx)
        if action == "validate":
            print(json.dumps({"valid": True, "enabled": cfg["enabled"], "mode": cfg["mode"]}, indent=2))
            return 0
        if action == "status" and not cfg["enabled"]:
            print(json.dumps({"enabled": False, "configured": False, "mode": cfg["mode"], "store": None}, indent=2))
            return 0
        if action == "run-once" and not cfg["enabled"]:
            print(json.dumps({"error": "plugin_disabled"}))
            return 1
        from . import _store

        store = _store(ctx)
        if action == "status":
            print(json.dumps(store.status(), indent=2, sort_keys=True))
            return 0
        if action == "run-once":
            synced = sync_configured_sources(store, cfg)
            scheduler = Scheduler(store, cfg, worker_id=f"cli:{ctx.profile_name}")
            result = asyncio.run(scheduler.tick())
            print(json.dumps({"synced": synced, "cycle": result}, indent=2, sort_keys=True))
            return 0
        print(f"Unknown proactive action: {action}")
        return 2
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

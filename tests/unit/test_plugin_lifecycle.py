from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

import proactive


class FakeContext:
    profile_name = "isolated-test"

    def __init__(self):
        self.spawn_count = 0

    def get_config(self, key, default=None):
        values = {
            "enabled": True,
            "mode": "shadow",
            "owner": {"id": "owner-test"},
            "scheduler": {"tick_seconds": 60},
        }
        return values.get(key, default)

    def spawn_task(self, coroutine, name=None):
        self.spawn_count += 1
        return asyncio.create_task(coroutine, name=name)


class DummyStore:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def disable_missing_configured_sources(self, active_source_ids, now):
        return 0


def test_status_when_disabled_does_not_open_store(monkeypatch):
    class DisabledContext:
        profile_name = "isolated-test"

        def get_config(self, key, default=None):
            return default

    context = DisabledContext()
    monkeypatch.setattr(proactive, "_store", lambda ctx: (_ for _ in ()).throw(AssertionError("store must stay closed")))

    payload = json.loads(proactive.status(context))

    assert payload["enabled"] is False
    assert payload["configured"] is False
    assert payload["store"] is None


def test_scheduler_starts_once_and_cleanup_cancels_it(monkeypatch):
    async def scenario():
        context = FakeContext()
        store = DummyStore()
        monkeypatch.setattr(proactive, "_store", lambda ctx: store)

        proactive.on_session_start(context)
        proactive.on_session_start(context)

        assert context.spawn_count == 1
        task = proactive._RUNTIME[id(context)]["task"]
        assert not task.done()

        proactive.cleanup(context)
        await asyncio.sleep(0)

        assert task.cancelled()
        assert store.closed
        assert id(context) not in proactive._RUNTIME

    asyncio.run(scenario())


def test_scheduler_delegates_to_host_without_local_event_loop(monkeypatch):
    class HostTask:
        cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    class SyncContext(FakeContext):
        def spawn_task(self, coroutine, name=None):
            self.spawn_count += 1
            coroutine.close()
            return HostTask()

    context = SyncContext()
    store = DummyStore()
    monkeypatch.setattr(proactive, "_store", lambda ctx: store)

    proactive.on_session_start(context)

    assert context.spawn_count == 1
    assert id(context) in proactive._RUNTIME
    proactive.cleanup(context)
    assert store.closed


def test_spawn_failure_is_fail_closed_and_releases_store(monkeypatch):
    async def scenario():
        context = FakeContext()
        store = DummyStore()
        monkeypatch.setattr(proactive, "_store", lambda ctx: store)

        def fail_spawn(coroutine, name=None):
            raise OSError("host task registry unavailable")

        context.spawn_task = fail_spawn
        proactive.on_session_start(context)

        assert store.closed
        assert id(context) not in proactive._RUNTIME

    asyncio.run(scenario())


def test_store_requires_reliable_profile_identity(tmp_path, monkeypatch):
    class MissingProfileContext:
        def get_config(self, key, default=None):
            if key == "owner":
                return {"id": "owner-test"}
            return default

    context = MissingProfileContext()
    monkeypatch.setattr(proactive, "get_hermes_home", lambda: tmp_path)

    with pytest.raises(ValueError, match="profile identity"):
        proactive._store(context)


def test_telegram_factory_starts_scheduler_after_transport_registration(monkeypatch):
    calls = []

    class CommandHandler:
        def __init__(self, command, callback):
            self.command = command
            self.callback = callback

    class Native:
        bot = object()

        def __init__(self):
            self.handlers = []

        def add_handler(self, handler, group=None):
            self.handlers.append((handler, group))

    telegram_module = types.ModuleType("telegram")
    telegram_ext_module = types.ModuleType("telegram.ext")
    telegram_ext_module.CommandHandler = CommandHandler
    monkeypatch.setitem(sys.modules, "telegram", telegram_module)
    monkeypatch.setitem(sys.modules, "telegram.ext", telegram_ext_module)
    monkeypatch.setattr(proactive, "telegram_sender", lambda bot: lambda *args, **kwargs: None)
    monkeypatch.setattr(proactive, "on_session_start", lambda ctx, **kwargs: calls.append(ctx))
    context = object()
    native = Native()

    proactive.telegram_factory(context)(native, object())

    assert calls == [context]
    assert native.handlers[0][0].command == "proactive_verify"

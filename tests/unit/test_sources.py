from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

from conftest import NOW, config

from radar.ingress.gmail_push_receiver import GmailPushReceiver, hermes_headers
from radar.ingress.webhook_consumer import WebhookConsumer, gmail_push_of
from radar.providers.base import MailMessage, ProviderError
from radar.providers.discovery import discover, load_schema_cache, suggest_sources
from radar.providers.mail_gmail_mcp import GmailMcpProvider
from radar.providers.registry import classify_tools
from radar.sources.gmail import GmailSource
from radar.sources.session import SessionDbPoller, record_turn

SEARCH_TEXT = """ID: m1
Subject: Confirmación de reserva ABC123
From: TAP <noreply@flytap.com>
Date: Mon, 19 Oct 2026 09:00:00 +0200

ID: m2
Subject: Newsletter semanal: ofertas
From: Shop <news@shop.example>
Date: Mon, 19 Oct 2026 09:05:00 +0200
"""
READ_TEXT = """Thread ID: th-1
Message-ID: <x@y>
Subject: Confirmación de reserva ABC123
From: TAP <noreply@flytap.com>
To: a@example.org
Date: Mon, 19 Oct 2026 09:00:00 +0200

Su vuelo Madrid-Lisboa del 3 de noviembre está confirmado. Localizador ABC123."""


def fake_call_mcp(server, tool, arguments, timeout=30):
    if tool == "search_emails":
        return {"ok": True, "result": {"content": [{"type": "text", "text": SEARCH_TEXT}]}}
    if tool == "read_email":
        return {"ok": True, "result": {"content": [{"type": "text", "text": READ_TEXT}]}}
    if tool == "get_thread":
        return {"ok": True, "result": {"content": [{"type": "text", "text": "thread"}]}}
    return {"ok": False, "error": "unknown tool"}


def test_gmail_provider_parses_search_read_and_refuses_writes():
    provider = GmailMcpProvider(fake_call_mcp, "gmail-a")
    found = provider.search("in:inbox", max_results=10)
    assert [m.id for m in found] == ["m1", "m2"] and found[0].epoch is not None
    message = provider.read("m1")
    assert message.thread_id == "th-1" and "ABC123" in message.body and message.sender.endswith("flytap.com>")
    try:
        provider.send(to=["x@y"], subject="s", body="b")
        assert False, "write must be refused"
    except PermissionError:
        pass
    truncated = {"ok": True, "result": '{"result": "Thread ID: th-9\\nMessage-ID: <a>\\nSubject: Big\\nFrom: a@b\\nTo: c@d\\nDate: Mon, 19 Oct 2026 09:00:00 +0200\\n\\nbody… [truncated]'}
    detail = GmailMcpProvider(lambda *a, **k: truncated, "gmail-a").read("m9")
    assert detail.subject == "Big" and detail.body.startswith("body")


def test_gmail_source_sync_triages_reads_and_advances_cursor(store):
    cfg = config()
    store.sync_source(source_id="gmail-a", kind="gmail", account_ref="a@example.org", config={"address": "a@example.org", "server": "gmail-a"},
                      resource_scope={"accounts": ["a@example.org"]}, retention_days=30, enabled=True, operations=["gmail.read"], now="2026-10-19T08:00:00Z")
    row = store.get_source("configured:gmail-a")
    source = GmailSource(row, cfg, store, provider=GmailMcpProvider(fake_call_mcp, "gmail-a"), clock=lambda: NOW)
    assert "newer_than:24h" in source._query(NOW)
    result = source.sync(NOW)
    assert (result.added, result.ignored, result.seen) == (1, 1, 2) and result.error is None
    kept = store.get_signal([s["id"] for s in store.list_signals(status="pending")][0])
    assert kept["group_key"] == "thread:a@example.org:th-1" and "ABC123" in kept["raw_content"] and kept["meta"]["automation"] == ["noreply-sender"]
    ignored = store.list_signals(status="ignored")[0]
    assert ignored["triage_reason"] == "noise_subject"
    assert result.cursor["after_epoch"] > 0
    store.update_source_cursor(source.id, result.cursor, "2026-10-19T10:01:00Z")
    source.row = store.get_source(source.id)
    assert "after:" in source._query(NOW)
    again = source.sync(NOW)
    assert again.added == 0 and again.seen == 2

    class Broken(GmailMcpProvider):
        def search(self, query, *, max_results=20):
            raise ProviderError("boom")

    broken = GmailSource(row, cfg, store, provider=Broken(fake_call_mcp, "gmail-a"), clock=lambda: NOW).sync(NOW)
    assert broken.error == "boom" and broken.health["status"] == "error"


def test_record_turn_debounces_and_skips_cron_platforms(store):
    cfg = config()
    event = {"session_id": "sess-1", "turn_id": "turn-1", "platform": "telegram", "user_message": "busca RAM DDR5 32GB", "assistant_response": "Vale, miro opciones", "model": "m"}
    assert record_turn(store, cfg, event, NOW)
    assert not record_turn(store, cfg, event, NOW)
    assert not record_turn(store, cfg, {**event, "turn_id": "turn-2", "platform": "cron"}, NOW)
    signal = store.list_signals()[0]
    assert signal["group_key"] == "session:sess-1" and signal["next_attempt_at"] == "2026-10-19T10:02:00Z" and signal["meta"]["assistant_text"] == "Vale, miro opciones"
    assert store.claim_signal_bundles("2026-10-19T10:01:00Z", "2026-10-19T10:11:00Z", 5) == []


def test_session_db_poller_pairs_turns_and_keeps_cursor(store):
    cfg = config(observation={"local_sessions": True, "state_db_poll_seconds": 300, "debounce_seconds": 0})
    store.ensure_local_observation_source("2026-10-19T10:00:00Z", 30)

    class FakeDb:
        def list_sessions_rich(self, **kwargs):
            return [{"id": "sess-9", "source": "cli"}]

        def get_messages(self, session_id, **kwargs):
            after = kwargs.get("after_id")
            rows = [{"id": 1, "role": "user", "content": "reserva hotel en Lisboa para el 3"}, {"id": 2, "role": "assistant", "content": "Busco opciones"},
                    {"id": 3, "role": "user", "content": "ok"}, {"id": 4, "role": "assistant", "content": "hecho"}]
            return [r for r in rows if not after or r["id"] > after]

        def close(self):
            pass

    poller = SessionDbPoller(store.get_source("local:session-hooks"), cfg, store, db_factory=FakeDb, clock=lambda: NOW)
    result = poller.sync(NOW)
    assert result.added == 2 and result.cursor["sessions"] == {"sess-9": 4}
    poller.row = {**poller.row, "cursor": result.cursor}
    assert poller.sync(NOW).added == 0


def test_webhook_consumer_skips_own_routes_only():
    pushes = []
    consumer = WebhookConsumer({"gmail-push": {"kind": "gmail_push", "skip": True}}, on_gmail_push=pushes.append)
    event = SimpleNamespace(source=SimpleNamespace(user_id="webhook:gmail-push"), raw_message={"emailAddress": "A@Example.org", "historyId": "77"}, message_id="d1")
    assert consumer.on_event(event) == {"action": "skip", "reason": "radar: gmail push consumed"}
    assert pushes == [{"address": "a@example.org", "history_id": "77", "pubsub_id": ""}]
    other = SimpleNamespace(source=SimpleNamespace(user_id="webhook:github-pr"), raw_message={}, message_id="d2")
    assert consumer.on_event(other) is None
    assert gmail_push_of({"nope": 1}) is None


def test_push_receiver_decodes_dedupes_and_forwards_signed():
    forwarded = []

    async def forward(url, body, headers):
        forwarded.append((url, json.loads(body), headers))

    receiver = GmailPushReceiver(token="tok", public_url="https://x.example/push", hermes_webhook_url="http://127.0.0.1:8644/webhooks/gmail-push",
                                 hermes_secret="s3cret", allowed_accounts={"a@example.org"}, forward=forward)
    data = base64.b64encode(json.dumps({"emailAddress": "A@example.org", "historyId": 123}).encode()).decode()
    payload = receiver.decode({"message": {"data": data, "messageId": "pm-1", "publishTime": "t"}, "subscription": "sub"})
    assert payload == {"emailAddress": "a@example.org", "historyId": "123", "pubsubMessageId": "pm-1", "publishTime": "t"}
    assert receiver._remember("pm-1") and not receiver._remember("pm-1")
    asyncio.run(receiver.forward(payload))
    url, body, headers = forwarded[0]
    assert body["historyId"] == "123" and headers["X-Request-ID"] == "pm-1" and headers["X-Webhook-Signature-V2"]
    assert hermes_headers("s3cret", b"{}")["X-Webhook-Timestamp"].isdigit()
    try:
        receiver.decode({"message": {"data": base64.b64encode(b'{"emailAddress": "other@x"}').decode(), "messageId": "pm-2"}})
        assert False, "foreign account must be rejected"
    except PermissionError:
        pass


def test_discovery_classifies_servers_from_schema_cache(tmp_path, monkeypatch):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "mcp_schema_cache.json").write_text(json.dumps({
        "gmail-x": {"fingerprint": "f", "tools": [{"name": n} for n in ("search_emails", "read_email", "get_thread")]},
        "cal": {"fingerprint": "f", "tools": [{"name": n} for n in ("list-events", "search-events")]},
        "other": {"fingerprint": "f", "tools": [{"name": "do_thing"}]},
    }))
    monkeypatch.setattr("radar.providers.discovery.load_mcp_servers", lambda home: {
        "gmail-x": {"command": "npx", "args": ["--yes", "@artymclabin/gmail-mcp@1.2.3"]},
        "cal": {"command": "npx", "args": ["--yes", "@cocal/google-calendar-mcp@2.6.3", "start"]},
        "other": {"command": "x"}, "missing": {"command": "y"},
    })
    assert classify_tools(["search_emails", "read_email", "get_thread"]) == "gmail_mcp"
    rows = {r["server"]: r for r in discover(tmp_path)}
    assert rows["gmail-x"]["provider"] == "gmail_mcp" and rows["gmail-x"]["package"].startswith("@artymclabin")
    assert rows["cal"]["kind"] == "calendar" and rows["other"]["provider"] is None and rows["missing"]["cached"] is False
    suggestions = suggest_sources(list(rows.values()))
    assert sorted(s["kind"] for s in suggestions) == ["calendar", "gmail"]
    assert load_schema_cache(tmp_path / "nowhere") == {}

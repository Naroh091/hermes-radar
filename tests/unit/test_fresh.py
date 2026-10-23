"""What the owner already answered is not asked again: their sent mail in a tracked thread is a signal, and a
message about a thread re-reads it before going out."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from conftest import NOW, FakeContext, active_config, config, verify_destination

from radar.providers.base import MailMessage, MailProvider
from radar.runtime import Runtime
from radar.scheduler import Scheduler
from radar.sources.gmail import GmailSource, address_of
from radar.transports import DeliveryReceipt, TransportRegistry

NOW_S = "2026-10-19T10:00:00Z"
OWNER = "a@example.org"


class Provider(MailProvider):
    provider_id = "fake"

    def __init__(self, messages):
        self.messages = list(messages)
        self.queries: list[str] = []

    def search(self, query, *, max_results=20):
        self.queries.append(query)
        # Like the real listing: no thread id and no body until the message is read.
        return [MailMessage(id=m.id, subject=m.subject, sender=m.sender, date=m.date, epoch=m.epoch) for m in self.messages]

    def read(self, message_id):
        self.reads = getattr(self, "reads", []) + [message_id]
        return next(m for m in self.messages if m.id == message_id)


def _message(mid, sender, subject, body, *, thread="th-1", epoch=1760867000):
    return MailMessage(id=mid, thread_id=thread, subject=subject, sender=sender, to="", date="", epoch=epoch, snippet=body[:100], body=body)


def _source(store, cfg, provider):
    store.sync_source(source_id="gmail-a", kind="gmail", account_ref=OWNER, config={"address": OWNER, "server": "gmail-a"},
                      resource_scope={"accounts": [OWNER]}, retention_days=30, enabled=True, operations=["gmail.read"], now="2026-10-19T08:00:00Z")
    return GmailSource(store.get_source("configured:gmail-a"), cfg, store, provider=provider, clock=lambda: NOW)


def _tracked_task(store, source, task_id="t1"):
    """A task born from Sebastián's mail in thread th-1, with a question sent to the owner about it."""
    report = _message("m1", "Sebastián <sebas@factchequeado.com>", "problema con carga de borrador", "Ana María no puede abrir el artículo 3182.")
    assert source.ingest_message(report, NOW) == "added"
    signal = store.list_signals(group_key=f"thread:{OWNER}:th-1")[0]
    store.finish_signals([signal], status="extracted", reason="new", now=NOW_S)
    store.create_task(task_id=task_id, kind="work_item", title="Fallo al cargar borrador", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    store.link_task_signal(task_id, signal["id"], "origin", NOW_S)
    store.create_task_proposal(proposal_id="ask", task_id=task_id, title="¿Quién opera el alojamiento?", reason_text="", action_class="read", operation_id="ask",
                               params={}, options=[{"id": "si", "label": "Nosotros"}, {"id": "no", "label": "Externo"}], urgency="high",
                               expires_at="2026-10-22T10:00:00Z", fingerprint="ask", now=NOW_S, batch_key="now")
    return signal


def test_the_owners_sent_mail_counts_only_where_a_task_is_waiting(store):
    cfg = config()
    provider = Provider([])
    source = _source(store, cfg, provider)
    assert "{in:inbox in:sent}" in source._query(NOW) and address_of("David <A@Example.org>") == OWNER
    # A mail the owner sent in a thread nobody tracks is noted and ignored.
    unrelated = _message("s1", f"David <{OWNER}>", "Comida del viernes", "¿Nos vemos a las 14?", thread="th-9")
    assert source.ingest_message(unrelated, NOW) == "ignored"
    assert store.list_signals(group_key=f"thread:{OWNER}:th-9")[0]["triage_reason"] == "owner_sent_untracked"
    # Their reply in a tracked thread is a signal the planner will judge, marked as the owner's own. The search
    # listing does not say which thread a message belongs to, so the message is read first (that is how the real
    # reply of 2026-10-23 was first filed under a loose key and ignored).
    _tracked_task(store, source)
    provider.messages = [_message("s2", f"David <{OWNER}>", "Re: problema con carga de borrador", "Ya lo estamos mirando, era Varnish.", epoch=1760867300)]
    assert source.sync(NOW).added == 1 and provider.reads == ["s2"]
    pending = store.list_signals(group_key=f"thread:{OWNER}:th-1", status="pending")
    assert len(pending) == 1 and pending[0]["meta"]["owner_sent"] is True and pending[0]["meta"]["sender"].endswith(f"<{OWNER}>")
    assert "Varnish" in pending[0]["snippet"]


def test_the_owners_reply_moves_the_task_on_without_a_word_back(store, tmp_path):
    from conftest import FakeLlm, plan_answer

    from radar.pipeline.process import process_bundle

    cfg = active_config()
    verify_destination(store)
    source = _source(store, cfg, Provider([]))
    _tracked_task(store, source)
    store.update_task("t1", now=NOW_S, bump_version=False, entities={"draft": {"text": "Hola Sebastián, lo miramos.", "ref": "", "at": NOW_S}})
    reply = _message("s2", f"David <{OWNER}>", "Re: problema con carga de borrador", "Ya lo hemos mirado: era Varnish, lo reinicio ahora.", epoch=1760867300)
    assert source.ingest_message(reply, NOW) == "added"
    plan = plan_answer(task_match={"decision": "advances", "task_id": "t1", "why": "the owner answered Sebastián"},
                       task={**plan_answer()["task"], "kind": "work_item", "title": "Fallo al cargar borrador", "status": "waiting"},
                       checklist=[], proposals=[], notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"},
                       message="Has respondido a Sebastián.", escalation={"decision": "inform", "why": "the owner knows"})
    llm = FakeLlm({"radar.plan": [plan]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    [bundle] = store.claim_signal_bundles(NOW_S, "2026-10-19T10:10:00Z", 5)
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome["task_id"] == "t1" and outcome["disposition"] == "silent"  # bound to the task, no extraction call, nothing sent
    assert [c["purpose"] for c in llm.calls] == ["radar.plan"] and '"owner_reply"' in llm.calls[0]["input"][0]["text"]
    assert store.get_proposal("ask")["status"] == "expired" and store.get_proposal("ask")["params"]["superseded_by"] == "replan"
    task = store.get_task("t1")
    assert task["status"] == "waiting" and "draft" not in task["entities"]
    assert store.list_signals(group_key=f"thread:{OWNER}:th-1", status="pending") == [] and store.list_outbox(limit=5) == []
    assert "owner_reply" in reason_prompt()


def reason_prompt() -> str:
    from radar.pipeline.reason import PLAN_PROMPT

    return PLAN_PROMPT


def test_a_message_about_a_thread_that_moved_waits_and_a_withdrawn_one_never_goes_out(store, tmp_path):
    cfg = active_config()
    verify_destination(store)
    reply = _message("s2", f"David <{OWNER}>", "Re: problema con carga de borrador", "Ya lo hemos mirado, era Varnish.", epoch=1760867300)
    provider = Provider([reply])
    source = _source(store, cfg, provider)
    _tracked_task(store, source)
    verify_destination(store)
    store.enqueue_notification(outbox_id="o1", destination_id="configured:primary", followup_id=None, proposal_id=None, notification_key="notice:t1",
                               payload={"text": "¿Quién opera el alojamiento?", "parse_mode": None, "buttons": [], "proposal_ids": ["ask"], "kind": "notice"},
                               now=NOW_S, task_id="t1", kind="notice")
    store.mark_proposals_sent(["ask"], "o1", NOW_S)
    sent = []
    transports = TransportRegistry()

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt("msg-1")

    transports.register("telegram", sender)
    rt = Runtime(FakeContext(cfg), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    rt.sources = {source.id: source}
    scheduler = Scheduler(store, cfg, worker_id="w", transports=transports, clock=lambda: NOW, runtime=rt)
    stats = asyncio.run(scheduler.tick())
    # The thread was re-read, the owner's reply is waiting to be judged, so the question is held and nothing is sent.
    assert stats["delivered"] == 0 and sent == [] and any("in:sent" in q for q in provider.queries)
    row = store.list_outbox(limit=5)[0]
    assert row["status"] == "failed" and row["last_error"] == "thread_moved" and row["next_attempt_at"] == "2026-10-19T10:10:00Z"
    # The re-plan judges the reply and withdraws the question: the held message is cancelled, never delivered.
    [pending] = store.list_signals(group_key=f"thread:{OWNER}:th-1", status="pending")
    store.finish_signals([pending], status="extracted", reason="advances", now="2026-10-19T10:05:00Z")
    assert store.supersede_proposals(["ask"], now="2026-10-19T10:05:00Z") == 1
    later = NOW + timedelta(minutes=11)
    scheduler.clock = lambda: later
    assert asyncio.run(scheduler.tick())["delivered"] == 0 and sent == []
    row = store.list_outbox(limit=5)[0]
    assert row["status"] == "cancelled" and row["last_error"] == "superseded"
    # A thread with nothing new is delivered as usual.
    provider.messages = []
    store.create_task(task_id="t2", kind="work_item", title="Otra", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    store.create_task_proposal(proposal_id="ask2", task_id="t2", title="¿Seguimos?", reason_text="", action_class="read", operation_id="ask", params={},
                               options=[{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}], urgency="normal", expires_at="2026-10-22T10:00:00Z",
                               fingerprint="ask2", now=NOW_S, batch_key="now")
    store.enqueue_notification(outbox_id="o2", destination_id="configured:primary", followup_id=None, proposal_id=None, notification_key="notice:t2",
                               payload={"text": "¿Seguimos?", "parse_mode": None, "buttons": [], "proposal_ids": ["ask2"], "kind": "notice"},
                               now="2026-10-19T10:11:00Z", task_id="t2", kind="notice")
    store.mark_proposals_sent(["ask2"], "o2", "2026-10-19T10:11:00Z")
    assert asyncio.run(scheduler.tick())["delivered"] == 1 and sent[0]["text"] == "¿Seguimos?"

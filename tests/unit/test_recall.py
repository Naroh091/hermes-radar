from __future__ import annotations

import asyncio
import json
import time

from conftest import NOW, FakeContext, FakeLlm, active_config, config, extract_answer, plan_answer

from radar.pipeline import reason
from radar.pipeline.process import process_bundle, review_task
from radar.pipeline.recall import MemoryRecall, Recall, build_query, parse_sessions, parse_tool_result, pick_tool
from radar.runtime import Runtime

_Q = {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}
HINDSIGHT = [{"name": "hindsight_retain", "parameters": {"type": "object", "required": ["content"], "properties": {"content": {"type": "string"}}}},
             {"name": "hindsight_recall", "parameters": _Q}, {"name": "hindsight_reflect", "parameters": _Q}]
MNEMOSYNE = [{"name": "mnemosyne_shared_recall", "parameters": _Q}, {"name": "mnemosyne_graph_query", "parameters": _Q},
             {"name": "mnemosyne_recall", "parameters": _Q}, {"name": "mnemosyne_scratchpad_read", "parameters": {"type": "object", "properties": {}}}]
QUESTION = [{"name": "brv_curate", "parameters": {"type": "object", "required": ["content"], "properties": {"content": {"type": "string"}}}},
            {"name": "brv_query", "parameters": {"type": "object", "required": ["question"], "properties": {"question": {"type": "string"}}}}]


class FakeProvider:
    def __init__(self, schemas, answer, *, delay=0.0):
        self.schemas, self.answer, self.delay = schemas, answer, delay
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def get_tool_schemas(self):
        return self.schemas

    def handle_tool_call(self, name, args, **kwargs):
        self.calls.append((name, args))
        if self.delay:
            time.sleep(self.delay)
        return self.answer

    def queue_prefetch(self, query, *, session_id=""):
        self.calls.append(("queue_prefetch", {"query": query}))

    def prefetch(self, query, *, session_id=""):
        self.calls.append(("prefetch", {"query": query}))
        return self.answer

    def shutdown(self):
        self.closed = True


def test_pick_tool_finds_each_providers_recall_tool_and_honours_overrides():
    assert pick_tool(HINDSIGHT) == ("hindsight_recall", "query")
    assert pick_tool(MNEMOSYNE) == ("mnemosyne_recall", "query")
    assert pick_tool(QUESTION) == ("brv_query", "question")
    assert pick_tool(HINDSIGHT, "hindsight_reflect") == ("hindsight_reflect", "query")
    assert pick_tool(HINDSIGHT, "nope") is None and pick_tool([]) is None
    assert parse_tool_result(json.dumps({"result": "1. Prefiere ventanilla\n2. Vuela con TAP"})) == "1. Prefiere ventanilla\n2. Vuela con TAP"
    assert parse_tool_result(json.dumps({"result": "No relevant memories found."})) == ""
    assert parse_tool_result(json.dumps({"error": "Failed to search memory: boom"})) == ""
    assert parse_tool_result(json.dumps({"results": [{"text": "a"}]})) == '[{"text": "a"}]'
    assert parse_tool_result("plain text memories") == "plain text memories"


def test_sessions_are_parsed_from_the_host_envelope_and_bounded():
    raw = json.dumps({"success": True, "results": [
        {"session_id": "s1", "when": "2026-10-10 09:00", "title": "Viaje Lisboa", "snippet": "  busqué  hoteles cerca del Chiado ", "link": "x"},
        {"session_id": "", "snippet": "sin id"}, {"session_id": "s3", "snippet": ""},
        {"session_id": "s4", "when": "2026-10-11", "title": "", "preview": "p" * 900},
    ]})
    found = parse_sessions(raw, limit=3, per_item=100)
    assert [f["session_id"] for f in found] == ["s1", "s4"] and found[0]["snippet"] == "busqué hoteles cerca del Chiado"
    assert len(found[1]["snippet"]) == 100 and found[1]["snippet"].endswith("…")
    assert parse_sessions(json.dumps({"success": False, "error": "x"})) == [] and parse_sessions("not json") == []
    assert parse_sessions({"result": {"results": [{"session_id": "a", "snippet": "b"}]}}) == [{"session_id": "a", "when": "", "title": "", "snippet": "b"}]


def test_build_query_is_short_and_mentions_title_entities_and_senders():
    extract = extract_answer()
    signals = [{"title": "Confirmación de reserva TAP", "meta": {"sender": "TAP Air Portugal <noreply@tap.com>"}}]
    query = build_query(extract, signals, {"title": "Viaje a Lisboa", "entities": {"people": ["Ana"], "repo": "org/app"}})
    assert query.startswith("Viaje Lisboa Ana org/app") and "TAP Air Portugal" in query and len(query) <= 200  # one-letter words are dropped
    assert build_query(None, [], None) == ""


def test_gather_runs_memory_and_sessions_concurrently_and_survives_timeouts():
    cfg = config(context={"timeout_seconds": 1, "max_chars": 300, "session_results": 2})
    provider = FakeProvider(HINDSIGHT, json.dumps({"result": "El dueño prefiere hoteles con desayuno. " * 20}))
    searches: list[tuple[str, int]] = []

    def search(query, limit):
        searches.append((query, limit))
        return json.dumps({"success": True, "results": [{"session_id": f"s{i}", "when": "w", "title": "t", "snippet": "x" * 150} for i in range(5)]})

    recall = Recall(FakeContext(cfg), cfg, memory=MemoryRecall(cfg, provider=provider), search_fn=search)
    result = asyncio.run(recall.gather("Viaje a Lisboa"))
    assert provider.calls == [("hindsight_recall", {"query": "Viaje a Lisboa"})] and searches == [("Viaje a Lisboa", 2)]
    assert len(result["memory"]) == 300 and result["memory"].endswith("…")
    assert len(result["past_sessions"]) == 2 and sum(len(s["snippet"]) for s in result["past_sessions"]) <= 300
    # A slow backend returns nothing instead of stalling the pipeline; a provider without tools uses prefetch.
    slow = Recall(FakeContext(cfg), cfg, memory=MemoryRecall(cfg, provider=FakeProvider(HINDSIGHT, "late", delay=1.5)), search_fn=lambda q, l: time.sleep(1.5))

    async def timed():
        started = time.monotonic()
        return await slow.gather("q"), time.monotonic() - started

    empty, elapsed = asyncio.run(timed())  # asyncio.run itself then waits for the lingering worker threads
    assert empty["memory"] == "" and empty["past_sessions"] == [] and elapsed < 1.4
    plain = FakeProvider([], "# Memory\n- likes aisle seats")
    assert asyncio.run(MemoryRecall(cfg, provider=plain).recall("q", timeout=1)) == "# Memory\n- likes aisle seats"
    assert [c[0] for c in plain.calls] == ["queue_prefetch", "prefetch"]
    # Loader failures (no host, no provider) degrade to no memory, and close() shuts the provider down.
    broken = MemoryRecall(cfg, loader=lambda sid: (_ for _ in ()).throw(RuntimeError("no host")))
    assert asyncio.run(broken.recall("q", timeout=1)) == "" and broken.last_error == "no host"
    memory = MemoryRecall(cfg, provider=provider)
    memory.close()
    assert provider.closed and memory.provider is None
    # Disabled sections are skipped entirely.
    off = config(context={"memory": False, "sessions": False})
    assert asyncio.run(Recall(FakeContext(off), off, memory=MemoryRecall(off, provider=provider), search_fn=search).gather("q"))["memory"] == ""


def test_planner_input_carries_memory_and_past_sessions():
    recall = {"memory": "Prefiere ventanilla", "past_sessions": [{"session_id": "s1", "when": "w", "title": "t", "snippet": "buscó hoteles"}]}
    text = reason.build_input(now="2026-10-19T10:00:00Z", owner={}, trust_level="balanced", extract=None, signals=[], candidates=[], facts=[], preferences=[],
                              existing_task=None, recall=recall)
    payload = json.loads(text)
    assert payload["memory"] == "Prefiere ventanilla" and payload["past_sessions"][0]["session_id"] == "s1"
    assert "memory" not in json.loads(reason.build_input(now="n", owner={}, trust_level="balanced", extract=None, signals=[], candidates=[], facts=[],
                                                          preferences=[], existing_task=None, recall={"memory": "", "past_sessions": []}))
    assert "past_sessions" in reason.PLAN_PROMPT and "possibly stale" in reason.PLAN_PROMPT


def test_pipeline_recalls_before_planning_and_reviewing(store):
    cfg = active_config(trust={"level": "cautious", "overrides": {}, "auto_external": []})
    llm = FakeLlm({"radar.extract": [extract_answer()], "radar.plan": [plan_answer(proposals=[]), plan_answer(task_match={"decision": "advances", "task_id": "", "why": "r"}, proposals=[])]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    provider = FakeProvider(HINDSIGHT, json.dumps({"result": "Vuela siempre con TAP y prefiere ventanilla"}))
    rt.recall = Recall(rt.ctx, cfg, memory=MemoryRecall(cfg, provider=provider),
                       search_fn=lambda q, l: json.dumps({"success": True, "results": [{"session_id": "sess-9", "when": "w", "title": "Lisboa", "snippet": "miró hoteles en Alfama"}]}))
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now="2026-10-19T10:00:00Z")
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at="2026-10-19T10:00:00Z", external_ref="m1", group_key="g",
                     title="Confirmación de reserva", snippet="vuelo", meta={"sender": "TAP <noreply@tap.com>", "account": "a@example.org"}, fingerprint="f", now="2026-10-19T10:00:00Z")
    bundle = store.claim_signal_bundles("2026-10-19T10:00:00Z", "2026-10-19T10:10:00Z", 5)[0]
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome["recall"] == {"memory_chars": 43, "sessions": 1}
    plan_call = next(c for c in llm.calls if c.get("purpose") == "radar.plan")
    sent = json.dumps(plan_call, default=str, ensure_ascii=False)
    assert "prefiere ventanilla" in sent and "sess-9" in sent and "miró hoteles en Alfama" in sent
    assert provider.calls[0][1]["query"].startswith("Reserva") or "TAP" in provider.calls[0][1]["query"]
    # Reviewing an existing task recalls too; a broken recall never blocks the plan.
    task = store.get_task(outcome["task_id"])
    rt.recall = Recall(rt.ctx, cfg, memory=MemoryRecall(cfg, loader=lambda sid: (_ for _ in ()).throw(RuntimeError("down"))), search_fn=lambda q, l: 1 / 0)
    review = asyncio.run(review_task(rt, task, NOW))
    assert review["recall"] == {"memory_chars": 0, "sessions": 0} and "deferred" not in review


def test_recall_status_tolerates_name_properties_and_warm_loads_the_provider(store):
    cfg = config()
    rt = Runtime(FakeContext(cfg, llm=None), store, cfg, worker_id="w", clock=lambda: NOW)

    class Named(FakeProvider):
        @property
        def name(self):
            return "hindsight"

    provider = Named(HINDSIGHT, json.dumps({"result": "x"}))
    rt.recall = Recall(rt.ctx, cfg, memory=MemoryRecall(cfg, loader=lambda sid: provider), search_fn=None)
    assert rt.recall_status()["provider"] is None  # not loaded yet

    async def scenario():
        rt.warm_recall()
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    status = rt.recall_status()
    assert status["provider"] == "hindsight" and status["tool"] == "hindsight_recall" and status["sessions"] is False
    assert rt.ctx.spawned == ["radar:recall-warm"]


def test_memory_calls_run_inside_the_profile_secret_scope():
    import contextlib

    cfg = config()
    entered: list[str] = []

    @contextlib.contextmanager
    def scope():
        entered.append("in")
        yield
        entered.append("out")

    provider = FakeProvider(HINDSIGHT, json.dumps({"result": "ok"}))
    memory = MemoryRecall(cfg, loader=lambda sid: provider, scope=scope)

    async def scenario():
        await memory.warm()
        return await memory.recall("q", timeout=2)

    assert asyncio.run(scenario()) == "ok" and entered == ["in", "out", "in", "out"]

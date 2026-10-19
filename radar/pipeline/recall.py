"""Recalled context for the planner: the host's memory provider (whichever it is) and past chat sessions.

Provider-agnostic on purpose. Hermes exposes every memory provider through the same ``MemoryProvider``
contract (``get_tool_schemas()`` + ``handle_tool_call()``), so the provider's own search tool is used:
``hindsight_recall``, ``mnemosyne_recall``, a ``*_query`` tool with a ``question`` argument... When a
provider exposes no tools, ``queue_prefetch``/``prefetch`` is the fallback. Past sessions come from the
host's ``session_search`` tool. Everything recalled is data for the model, never instructions, and every
lookup is bounded by a timeout so a slow backend cannot stall the pipeline.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("radar.recall")

MAX_QUERY = 200
TOOL_PRIORITY = ("recall", "search", "retrieve", "query", "find")
# Tools that read something else than the owner's memories, or that write.
_SIDE_TOOLS = ("shared", "canonical", "graph", "triple", "scratchpad", "retain", "remember", "forget", "write",
               "store", "curate", "status", "stats", "sleep", "reflect", "delete")
_NO_RESULTS = re.compile(r"^\s*(no (relevant )?(memories|results|matches|memory)( found)?\.?|nothing (found|relevant)\.?)\s*$", re.I)


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# -- memory provider ---------------------------------------------------------------------------
def pick_tool(schemas: Any, override: str = "") -> tuple[str, str] | None:
    """``(tool name, query parameter)`` of the provider's search tool, or None when it has none."""
    tools: list[tuple[str, str, list[str]]] = []
    for schema in schemas or []:
        if not isinstance(schema, dict):
            continue
        name = str(schema.get("name") or "")
        params = schema.get("parameters") if isinstance(schema.get("parameters"), dict) else {}
        props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
        required = [r for r in (params.get("required") or []) if isinstance(r, str)]
        if not name or not props:
            continue
        param = ""
        if isinstance(props.get("query"), dict):
            param = "query"
        else:
            strings = [k for k in required if isinstance(props.get(k), dict) and props[k].get("type") == "string"]
            if len(strings) == 1:
                param = strings[0]
        if param:
            tools.append((name, param, required))
    if override:
        return next(((name, param) for name, param, _ in tools if name == override), None)

    def rank(entry: tuple[str, str, list[str]]) -> tuple[int, int, int, int]:
        name, param, required = entry
        lowered = name.lower()
        priority = next((i for i, key in enumerate(TOOL_PRIORITY) if key in lowered), len(TOOL_PRIORITY))
        side = sum(1 for word in _SIDE_TOOLS if word in lowered)
        return (priority, side, len([r for r in required if r != param]), len(name))

    ranked = sorted((t for t in tools if any(key in t[0].lower() for key in TOOL_PRIORITY)), key=rank)
    return (ranked[0][0], ranked[0][1]) if ranked else None


def parse_tool_result(raw: Any) -> str:
    """Text of a provider tool answer: ``{"result": ...}`` envelopes, JSON lists or plain text; errors → ''."""
    value: Any = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
    if isinstance(value, dict):
        if value.get("error"):
            return ""
        inner = next((value[key] for key in ("result", "results", "memories", "content", "text") if key in value), value)
        value = inner
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value or "")
    text = text.strip()
    return "" if not text or _NO_RESULTS.match(text) else text


def load_host_provider(session_id: str) -> Any:
    """The host's active memory provider, initialised for read-only recall; None when there is none."""
    from plugins.memory import load_memory_provider  # type: ignore[import-not-found]  # host package

    name = None
    try:
        from hermes_cli.config import load_config as host_config  # type: ignore[import-not-found]

        memory = (host_config() or {}).get("memory") or {}
        name = memory.get("provider") if isinstance(memory, dict) else None
    except Exception as exc:  # the host config is unreadable: no provider, not an error of ours
        log.debug("memory provider name not resolved: %s", exc)
    if not isinstance(name, str) or not name.strip():
        return None
    provider = load_memory_provider(name.strip(), register_skills=False)
    if provider is None:
        return None
    available = getattr(provider, "is_available", None)
    if callable(available) and not available():
        log.info("memory provider %s is not available: %s", name, getattr(provider, "unavailable_reason", lambda: "")())
        return None
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        home = str(get_hermes_home())
    except Exception:
        home = str(Path.home() / ".hermes")
    provider.initialize(session_id, hermes_home=home, platform="radar", agent_context="subagent")
    return provider


class MemoryRecall:
    """Lazy, provider-agnostic handle on the host's memory provider."""

    def __init__(self, config: dict[str, Any], *, provider: Any = None, loader: Callable[[str], Any] | None = None,
                 session_id: str = "radar-recall", scope: Callable[[], Any] | None = None):
        self.config = config
        self.scope = scope or contextlib.nullcontext  # the host's profile secret scope (multiplexed gateways)
        self.provider = provider
        self._loader = loader or load_host_provider
        self._loaded = provider is not None
        self._tool: tuple[str, str] | None = None
        self._tool_resolved = False
        self.session_id = session_id
        self.last_error = ""
        self.calls = 0

    def _settings(self) -> dict[str, Any]:
        return self.config.get("context") or {}

    def load(self) -> Any:
        if not self._loaded:
            self._loaded = True
            try:
                self.provider = self._loader(self.session_id)
            except Exception as exc:  # a missing host package or a failing backend: recall is optional
                self.last_error = str(exc)[:200]
                log.info("memory provider not loaded: %s", exc)
                self.provider = None
        return self.provider

    def tool(self) -> tuple[str, str] | None:
        if not self._tool_resolved:
            self._tool_resolved = True
            provider = self.load()
            if provider is not None:
                try:
                    schemas = provider.get_tool_schemas()
                except Exception:
                    schemas = []
                self._tool = pick_tool(schemas, str(self._settings().get("memory_tool") or ""))
        return self._tool

    def recall_sync(self, query: str) -> str:
        provider = self.load()
        if provider is None:
            return ""
        self.calls += 1
        tool = self.tool()
        if tool is not None:
            name, param = tool
            return parse_tool_result(provider.handle_tool_call(name, {param: query}))
        queue = getattr(provider, "queue_prefetch", None)
        if callable(queue):
            queue(query, session_id=self.session_id)
        return parse_tool_result(provider.prefetch(query, session_id=self.session_id))

    async def recall(self, query: str, *, timeout: float) -> str:
        if not query:
            return ""
        try:
            with self.scope():  # to_thread copies the context, so the worker sees the profile's secrets
                return await asyncio.wait_for(asyncio.to_thread(self.recall_sync, query), timeout=timeout)
        except asyncio.TimeoutError:
            self.last_error = "timeout"
            log.info("memory recall timed out after %.0fs", timeout)
            return ""
        except Exception as exc:
            self.last_error = str(exc)[:200]
            log.info("memory recall failed: %s", exc)
            return ""

    async def warm(self) -> None:
        """Load the provider and resolve its search tool in a worker thread (startup, off the pipeline)."""
        try:
            with self.scope():
                await asyncio.to_thread(self.tool)
        except Exception as exc:
            self.last_error = str(exc)[:200]

    def close(self) -> None:
        provider, self.provider, self._loaded, self._tool_resolved = self.provider, None, False, False
        if provider is not None:
            try:
                provider.shutdown()
            except Exception:
                pass


# -- past sessions -----------------------------------------------------------------------------
def host_session_search(ctx: Any) -> Callable[..., Any] | None:
    """``fn(query, limit) -> raw payload`` on the host's session_search tool (in-process, else dispatched)."""
    try:
        from tools.session_search_tool import session_search  # type: ignore[import-not-found]  # host package

        return lambda query, limit: session_search(query=query, limit=limit)
    except Exception:
        dispatch = getattr(ctx, "dispatch_tool", None)
        if callable(dispatch):
            return lambda query, limit: dispatch("session_search", {"query": query, "limit": limit})
    return None


def parse_sessions(raw: Any, *, limit: int = 3, per_item: int = 400) -> list[dict[str, str]]:
    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if payload.get("success") is False:
        return []
    found: list[dict[str, str]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or "").strip()
        snippet = _clip(item.get("snippet") or item.get("preview") or item.get("excerpt"), per_item)
        if not session_id or not snippet:
            continue
        found.append({"session_id": session_id, "when": str(item.get("when") or "")[:32], "title": _clip(item.get("title"), 120), "snippet": snippet})
        if len(found) >= limit:
            break
    return found


# -- query and facade --------------------------------------------------------------------------
def build_query(extract: dict[str, Any] | None, signals: list[dict[str, Any]], existing_task: dict[str, Any] | None) -> str:
    """A short search query: the task's title and main entities, the senders and subjects behind it."""
    parts: list[str] = []
    for source in (existing_task or {}, extract or {}):
        parts.append(str(source.get("title") or ""))
        entities = source.get("entities") if isinstance(source.get("entities"), dict) else {}
        for key in ("people", "organizations", "items", "places"):
            parts.extend(str(v) for v in (entities.get(key) or [])[:3] if isinstance(v, str))
        if isinstance(entities.get("repo"), str):
            parts.append(entities["repo"])
    for signal in signals[:3]:
        meta = signal.get("meta") or {}
        sender = str(meta.get("sender") or "")
        parts.append(sender.split("<")[0].strip(' "') if "<" in sender else sender)
        parts.append(str(signal.get("title") or ""))
    seen: set[str] = set()
    words: list[str] = []
    for part in parts:
        for word in part.split():
            key = word.lower().strip(",.;:()[]«»\"'")
            if key and key not in seen and len(key) > 1:
                seen.add(key)
                words.append(word)
    return _clip(" ".join(words), MAX_QUERY).rstrip("…")


class Recall:
    """Gathers memory and past-session context for one planning step, bounded in time and size."""

    def __init__(self, ctx: Any, config: dict[str, Any], *, memory: MemoryRecall | None = None,
                 search_fn: Callable[..., Any] | None = None, hermes_home: Any = None):
        self.ctx = ctx
        self.config = config
        if memory is None:
            from ..hostscope import profile_secret_scope

            memory = MemoryRecall(config, scope=lambda: profile_secret_scope(hermes_home))
        self.memory = memory
        self.search_fn = search_fn if search_fn is not None else host_session_search(ctx)

    async def _sessions(self, query: str, *, limit: int, timeout: float) -> list[dict[str, str]]:
        if self.search_fn is None:
            return []
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(self.search_fn, query, limit), timeout=timeout)
        except asyncio.TimeoutError:
            log.info("session search timed out after %.0fs", timeout)
            return []
        except Exception as exc:
            log.info("session search failed: %s", exc)
            return []
        return parse_sessions(raw, limit=limit)

    async def gather(self, query: str) -> dict[str, Any]:
        settings = self.config.get("context") or {}
        result: dict[str, Any] = {"query": query, "memory": "", "past_sessions": []}
        if not query:
            return result
        timeout = float(settings.get("timeout_seconds") or 45)
        max_chars = int(settings.get("max_chars") or 1500)
        memory_job = self.memory.recall(query, timeout=timeout) if settings.get("memory", True) else None
        sessions_job = self._sessions(query, limit=int(settings.get("session_results") or 3), timeout=timeout) if settings.get("sessions", True) else None
        memory, sessions = await asyncio.gather(
            memory_job if memory_job is not None else asyncio.sleep(0, result=""),
            sessions_job if sessions_job is not None else asyncio.sleep(0, result=[]),
        )
        result["memory"] = _clip(memory, max_chars)
        kept: list[dict[str, str]] = []
        used = 0
        for item in sessions:
            room = max_chars - used
            if room <= 80:
                break
            item = {**item, "snippet": _clip(item["snippet"], min(len(item["snippet"]), room))}
            kept.append(item)
            used += len(item["snippet"]) + len(item["title"]) + 40
        result["past_sessions"] = kept
        return result


def recall_summary(recalled: dict[str, Any] | None) -> dict[str, int]:
    if not recalled:
        return {"memory_chars": 0, "sessions": 0}
    return {"memory_chars": len(recalled.get("memory") or ""), "sessions": len(recalled.get("past_sessions") or [])}

"""Run one bounded Hermes session per job through the public CLI (ported from mail-intelligence).

Each job is a child ``hermes chat`` process with a real turn budget (``--max-turns``), a real time
budget (``--run-budget``) and a wall-clock ceiling enforced here. The child never gets to execute
external effects: the prompt restricts it, the tool firewall (``radar.jobs.firewall``, fed through the
child's environment) refuses external tools before they run, and external operations go through consent.
"""
from __future__ import annotations

import asyncio
import json
import re
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from .firewall import child_env as firewall_env

log = logging.getLogger(__name__)

_FINAL_JSON_KEYS = {"summary", "findings", "refs", "proposed_reply", "next_steps", "confidence", "effects"}


@dataclass
class JobOutcome:
    status: str  # succeeded | failed | timed_out | unavailable
    summary: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    session_ref: str | None = None
    error: str | None = None
    exit_code: int | None = None


def parse_stream(stdout: str) -> dict[str, Any] | None:
    """Last ``{"type": "result"}`` record of the JSONL stream, or None."""
    record = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("type") == "result":
            record = item
    return record


def extract_final_json(text: str) -> dict[str, Any] | None:
    """The job's final JSON object: a fenced ```json block, the last balanced object in the text, or a
    bare object body (the host sometimes strips the outer braces of a message that is only JSON)."""
    text = text or ""
    for block in reversed(_FENCE.findall(text)):
        parsed = _object(block)
        if parsed is not None:
            return parsed
    end = len(text)
    while end > 0:
        close = text.rfind("}", 0, end)
        if close < 0:
            break
        depth = 0
        start = -1
        for index in range(close, -1, -1):
            char = text[index]
            if char == "}":
                depth += 1
            elif char == "{":
                depth -= 1
                if depth == 0:
                    start = index
                    break
        if start < 0:
            break
        parsed = _object(text[start:close + 1])
        if parsed is not None:
            return parsed
        end = close
    body = text.strip().strip("`").strip()
    if body.startswith('"') and '":' in body:
        return _object("{" + body.rstrip().rstrip(",") + "}") or _object("{" + body + "}")
    return None


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _object(candidate: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None

def build_argv(
    *, hermes_bin: str, max_turns: int, run_budget: int, workdir: str | None, worktree: bool,
    toolsets: list[str], skills: list[str], model: str | None, extra_args: tuple[str, ...] = (),
) -> list[str]:
    argv = [hermes_bin, "chat", "--query-file", "-", "--oneshot", "--format", "stream-json",
            "--max-turns", str(max_turns), "--run-budget", str(int(run_budget))]
    if workdir:
        argv += ["--in", workdir]
    if worktree:
        argv.append("--worktree")
    if toolsets:
        argv += ["-t", ",".join(toolsets)]
    if skills:
        argv += ["-s", ",".join(skills)]
    if model:
        argv += ["-m", model]
    argv.extend(extra_args)
    return argv


class JobRunner:
    def __init__(self, *, hermes_bin: str | None = None, max_turns: int = 20, max_seconds: int = 300,
                 model: str | None = None, env: dict[str, str] | None = None, extra_args: tuple[str, ...] = (),
                 spawn: Any = None):
        self.hermes_bin = hermes_bin or shutil.which("hermes") or "hermes"
        self.max_turns = int(max_turns)
        self.max_seconds = int(max_seconds)
        self.model = model
        self.env = env
        self.extra_args = tuple(extra_args)
        # Injectable for tests: ``spawn(argv, stdin_text, timeout) -> (code, stdout, stderr)``.
        self.spawn = spawn

    def _child_env(self, job: dict[str, Any] | None = None, allow: str = "") -> dict[str, str]:
        """The parent's environment plus the firewall's variables: the job marker, the step's class and, for a
        consented external step, the tools it may use. Hooks stay on so the firewall runs in the child."""
        env = dict(self.env) if self.env is not None else dict(os.environ)
        env.pop("HERMES_RADAR_JOB_ALLOW", None)
        env.update(firewall_env((job or {}).get("action_class"), allow))
        env.setdefault("HERMES_ACCEPT_HOOKS", "1")
        return env

    async def run(self, job: dict[str, Any], *, on_pid: Any = None, allow: str = "", turns: int | None = None,
                  seconds: int | None = None) -> JobOutcome:
        """``turns``/``seconds`` override the runner's budgets for this job (an investigation gets more room)."""
        timeout = float(seconds or self.max_seconds)
        argv = build_argv(
            hermes_bin=self.hermes_bin, max_turns=int(turns or self.max_turns), run_budget=int(timeout), workdir=job.get("workdir"),
            worktree=bool(job.get("worktree")), toolsets=list(job.get("toolsets") or []),
            skills=list(job.get("skills") or []), model=self.model, extra_args=self.extra_args,
        )
        prompt = str(job.get("prompt") or "")
        try:
            if self.spawn is not None:
                code, stdout, stderr = await self.spawn(argv, prompt, timeout + 30.0)
            else:
                code, stdout, stderr = await self._subprocess(argv, prompt, timeout + 30.0, job.get("workdir"), on_pid,
                                                              env=self._child_env(job, allow))
        except asyncio.TimeoutError:
            return JobOutcome("timed_out", error="time budget exhausted")
        except FileNotFoundError as exc:
            return JobOutcome("unavailable", error=f"hermes CLI not found: {exc}")
        except Exception as exc:
            return JobOutcome("failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        record = parse_stream(stdout)
        if record is None:
            tail = (stderr or "").strip()[-300:]
            return JobOutcome("failed", error="no result record in hermes chat output" + (f"; stderr: {tail}" if tail else ""), exit_code=code)
        text = str(record.get("text") or "").strip()
        final = extract_final_json(text) or {}
        summary = str(final.get("summary") or text)[:6000]
        anomaly = record.get("error") or (f"exit {record.get('exit_code')}" if record.get("exit_code") not in (0, None) else "")
        usage = {k: record.get(k) for k in ("usage", "tokens", "cost_usd", "turns", "duration_ms") if record.get(k) is not None}
        session_ref = str(record.get("session_id") or "") or None
        if not summary:
            return JobOutcome("failed", error=str(anomaly or "empty job result")[:300], exit_code=code, session_ref=session_ref)
        status = "succeeded"
        return JobOutcome(status, summary=summary, result={**final, "raw_tail": text[-2000:], "anomaly": str(anomaly or "")},
                          usage=usage, session_ref=session_ref, error=str(anomaly)[:300] if anomaly else None, exit_code=code)

    async def _subprocess(self, argv: list[str], stdin_text: str, timeout: float, cwd: str | None, on_pid: Any,
                          env: dict[str, str] | None = None) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cwd if cwd and os.path.isdir(cwd) else None, env=env if env is not None else self._child_env(), start_new_session=True,
        )
        if on_pid is not None:
            try:
                on_pid(process.pid)
            except Exception:
                pass
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin_text.encode("utf-8")), timeout=timeout)
        except asyncio.TimeoutError:
            await self._kill(process)
            raise
        except asyncio.CancelledError:
            await self._kill(process)
            raise
        return process.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")

    @staticmethod
    async def _kill(process: Any) -> None:
        try:
            import signal

            os.killpg(process.pid, signal.SIGTERM)
        except Exception:
            try:
                process.terminate()
            except Exception:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

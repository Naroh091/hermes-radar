from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

from conftest import NOW, config

from radar.jobs.runner import JobOutcome, JobRunner, build_argv, extract_final_json, parse_stream
from radar.jobs.service import enqueue_job_for_proposal, job_block_reason, record_job_outcome, release_orphans
from radar.scheduler import Scheduler
from radar.trust import may_auto_run
from radar.reversibility import classify, validate_no_automerge


def test_parse_stream_and_final_json():
    stdout = "\n".join([
        json.dumps({"type": "message", "text": "working"}),
        json.dumps({"type": "result", "text": 'Done.\n{"summary": "Found 3 hotels", "findings": ["a"], "refs": ["https://x"], "proposed_reply": "", "next_steps": [], "confidence": 0.8, "effects": []}', "session_id": "sess-1", "exit_code": 0}),
    ])
    record = parse_stream(stdout)
    assert record["session_id"] == "sess-1"
    final = extract_final_json(record["text"])
    assert final["summary"] == "Found 3 hotels" and final["refs"] == ["https://x"]
    assert extract_final_json("no json here {not: valid}") is None
    argv = build_argv(hermes_bin="hermes", max_turns=5, workdir="/repo", toolsets=["web"], skills=["s"], model=None)
    assert argv[:5] == ["hermes", "chat", "--query-file", "-", "--oneshot"]
    # The gateway rejects --run-budget and --worktree for a programmatic chat; a code job runs in --in.
    assert "--run-budget" not in argv and "--worktree" not in argv and argv[argv.index("--in") + 1] == "/repo"


def test_runner_with_fake_spawn_handles_success_timeout_and_missing_result():
    async def ok(argv, stdin, timeout):
        assert "Investigar" in stdin
        return 0, json.dumps({"type": "result", "text": '{"summary": "hecho", "findings": [], "refs": [], "proposed_reply": "", "next_steps": [], "confidence": 1, "effects": []}', "session_id": "s"}), ""

    async def slow(argv, stdin, timeout):
        raise asyncio.TimeoutError()

    async def empty(argv, stdin, timeout):
        return 1, "", "boom"

    job = {"id": "j", "prompt": "Investigar", "workdir": None}
    assert asyncio.run(JobRunner(spawn=ok, max_seconds=10).run(job)).status == "succeeded"
    assert asyncio.run(JobRunner(spawn=slow, max_seconds=10).run(job)).status == "timed_out"
    failed = asyncio.run(JobRunner(spawn=empty, max_seconds=10).run(job))
    assert failed.status == "failed" and "boom" in failed.error


def test_enqueue_job_and_record_outcome_produce_signal(store):
    cfg = config(workspaces={"org/app": "/tmp/app"}, limits={"jobs_per_day": 1})
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Bug en login", summary="500 al entrar", entities={"repo": "org/app"}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Investigar el bug", reason_text="hay traza", action_class="read", operation_id="investigate",
                               params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fp", now=now)
    store.respond_proposal("p1", decision="accepted", response={"option": "do"}, now=now)
    job = enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("p1"), option="do", now_dt=NOW)
    assert job["workdir"] == "/tmp/app" and job["worktree"] == 1 and "Bug en login" in job["prompt"] and "read-only" in job["prompt"]
    assert store.get_proposal("p1")["status"] == "executing" and store.get_task("t1")["status"] == "in_progress"
    assert enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("p1"), option="do", now_dt=NOW) is None  # one active job per task
    claimed = store.claim_queued_jobs(now, "2026-10-19T10:10:00Z", 2)[0]
    outcome = JobOutcome("succeeded", summary="Causa: null pointer", result={"findings": ["x"], "refs": ["PR #1"], "proposed_reply": "Hola"}, session_ref="sess")
    assert record_job_outcome(store, cfg, job=claimed, outcome=outcome, now_dt=NOW)
    signal = store.list_signals(kind="job_result")[0]
    assert signal["group_key"] == "task:t1" and signal["meta"]["refs"] == ["PR #1"] and signal["triage_status"] == "pending"
    assert store.get_proposal("p1")["status"] == "succeeded" and store.get_job(claimed["id"])["status"] == "succeeded"


def test_trust_and_reversibility_rules():
    balanced = {"level": "balanced", "overrides": {}, "auto_external": []}
    assert may_auto_run(balanced, "read") and may_auto_run(balanced, "local") and not may_auto_run(balanced, "external", operation_id="gmail.send_email")
    cautious = {"level": "cautious", "overrides": {"work_item": {"local": "auto"}}, "auto_external": []}
    assert not may_auto_run(cautious, "local") and may_auto_run(cautious, "local", kind="work_item")
    autonomous = {"level": "autonomous", "overrides": {}, "auto_external": ["gmail.reply", "github.comment"]}
    assert may_auto_run(autonomous, "external", operation_id="github.comment")  # a listed non-email external runs unattended
    assert not may_auto_run(autonomous, "external", operation_id="gmail.reply")  # email never auto, whatever auto_external lists
    assert not may_auto_run(autonomous, "external", operation_id="payments.charge")
    assert classify("github.open_pr") == "local" and classify("gmail.send_email") == "external" and classify("gmail.create_draft") == "external" and classify("whatever") == "external"
    try:
        validate_no_automerge("github.open_pr", {"auto_merge": True})
        assert False
    except ValueError:
        pass


def test_trust_guide_explains_every_level_in_both_languages():
    from radar.trust import guide_text, level_summary

    for language in ("es", "en"):
        text = guide_text(language)
        assert all(level in text for level in ("cautious", "balanced", "autonomous"))
        assert "read" in text and "local" in text and "external" in text
        assert "auto_external" in level_summary("autonomous", language)
    assert guide_text("fr") == guide_text("en")


def test_profile_secret_scope_binds_only_when_multiplexing_and_unscoped(tmp_path, monkeypatch):
    import sys
    import types

    from radar import hostscope

    calls = []
    fake = types.ModuleType("agent.secret_scope")
    state = {"scope": None, "multiplex": True}
    fake.is_multiplex_active = lambda: state["multiplex"]
    fake.current_secret_scope = lambda: state["scope"]
    fake.build_profile_secret_scope = lambda home: {"HERMES_CODEX_BASE_URL": "x", "home": str(home)}

    def set_secret_scope(secrets, *, profile_home=None):
        calls.append(("set", profile_home, secrets["home"]))
        state["scope"] = secrets
        return "tok"

    def reset_secret_scope(token):
        calls.append(("reset", token))
        state["scope"] = None

    fake.set_secret_scope = set_secret_scope
    fake.reset_secret_scope = reset_secret_scope
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake)
    hostscope._cache.clear()
    with hostscope.profile_secret_scope(tmp_path):
        assert state["scope"]["HERMES_CODEX_BASE_URL"] == "x"
        with hostscope.profile_secret_scope(tmp_path):  # already scoped: no second bind
            pass
    assert calls == [("set", str(tmp_path), str(tmp_path)), ("reset", "tok")]
    state["multiplex"] = False
    with hostscope.profile_secret_scope(tmp_path):
        assert state["scope"] is None
    assert len(calls) == 2


def _finish(store, cfg, at):
    from datetime import timedelta as _td

    claimed = store.claim_queued_jobs(at.isoformat().replace("+00:00", "Z"), (at + _td(minutes=10)).isoformat().replace("+00:00", "Z"), 2)[0]
    record_job_outcome(store, cfg, job=claimed, outcome=JobOutcome("succeeded", summary="ok", result={}), now_dt=at)


def test_owner_approved_external_step_runs_with_grant_but_never_automatically(store):
    cfg = config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Apuntar DNS", summary="CNAME pedido", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(
        proposal_id="p1", task_id="t1", title="Autorizar la creación del CNAME", reason_text="no existe", action_class="external", operation_id=None,
        params={}, options=[{"id": "autorizar_cambio", "label": "Sí, crea el CNAME"}, {"id": "no_autorizar", "label": "No"}],
        urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fp", now=now,
    )
    assert job_block_reason(store, cfg, proposal=store.get_proposal("p1"), now_dt=NOW, auto=True) == "external_auto"
    assert enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("p1"), option="autorizar_cambio", now_dt=NOW, auto=True) is None
    assert store.list_jobs() == []
    store.respond_proposal("p1", decision="accepted", response={"option": "autorizar_cambio", "actor": "telegram:7", "channel": "telegram"}, now=now)
    job = enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("p1"), option="autorizar_cambio", now_dt=NOW)
    assert job is not None and job["action_class"] == "external"
    assert "OWNER APPROVAL" in job["prompt"] and "«Sí, crea el CNAME»" in job["prompt"] and "exactly one external action" in job["prompt"]
    assert store.get_proposal("p1")["status"] == "executing"


def test_automatic_jobs_respect_cap_cooldown_and_budget_but_owner_bypasses_the_cap(store):
    from datetime import timedelta

    cfg = config(jobs={"max_auto_per_task": 2, "auto_cooldown_minutes": 15}, limits={"jobs_per_day": 3})
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Bug", summary="", entities={}, expectations=[], checklist=[], now=now)
    for index in range(1, 5):
        store.create_task_proposal(proposal_id=f"p{index}", task_id="t1", title=f"Paso {index}", reason_text="", action_class="read", operation_id="investigate",
                                   params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint=f"fp{index}", now=now)

    def accept(pid):
        store.respond_proposal(pid, decision="accepted", response={"option": "do"}, now=now)
        return store.get_proposal(pid)

    reason = lambda pid, at, auto: job_block_reason(store, cfg, proposal=store.get_proposal(pid), now_dt=at, auto=auto)  # noqa: E731
    assert enqueue_job_for_proposal(store, cfg, proposal=accept("p1"), option="do", now_dt=NOW, auto=True)
    assert reason("p2", NOW, True) == "active_job"
    _finish(store, cfg, NOW)
    assert reason("p2", NOW + timedelta(minutes=5), True) == "cooldown"
    assert reason("p2", NOW + timedelta(minutes=16), True) is None
    assert enqueue_job_for_proposal(store, cfg, proposal=accept("p2"), option="do", now_dt=NOW + timedelta(minutes=16), auto=True)
    _finish(store, cfg, NOW + timedelta(minutes=16))
    assert reason("p3", NOW + timedelta(minutes=40), True) == "task_cap"
    assert reason("p3", NOW + timedelta(minutes=40), False) is None  # the owner's approval is not capped
    assert enqueue_job_for_proposal(store, cfg, proposal=accept("p3"), option="do", now_dt=NOW + timedelta(minutes=40))
    _finish(store, cfg, NOW + timedelta(minutes=40))
    assert reason("p4", NOW + timedelta(minutes=60), False) == "budget"


def test_job_cancelled_by_a_restart_requeues_its_step_instead_of_failing(store):
    cfg = config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Bug", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Investigar", reason_text="", action_class="read", operation_id="investigate",
                               params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fp", now=now)
    store.respond_proposal("p1", decision="accepted", response={"option": "do", "auto": True}, now=now)
    assert enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("p1"), option="do", now_dt=NOW, auto=True)
    claimed = store.claim_queued_jobs(now, "2026-10-19T10:10:00Z", 1)[0]
    assert record_job_outcome(store, cfg, job=claimed, outcome=JobOutcome("cancelled", error="runtime stopped"), now_dt=NOW) is False
    proposal = store.get_proposal("p1")
    assert proposal["status"] == "waiting" and proposal["params"]["wait_reason"] == "interrupted"
    assert store.get_job(claimed["id"])["status"] == "cancelled" and store.list_signals(kind="job_result") == []


def test_autonomous_runs_listed_externals_unattended_but_never_email(store):
    cfg = config(trust={"level": "autonomous", "overrides": {}, "auto_external": ["gmail.reply", "github.comment"]})
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Bug", summary="", entities={}, expectations=[], checklist=[], now=now)
    for pid, operation in (("reply", "gmail.reply"), ("deploy", "deploy.run"), ("comment", "github.comment")):
        store.create_task_proposal(proposal_id=pid, task_id="t1", title=f"Paso {pid}", reason_text="", action_class="external", operation_id=operation,
                                   params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint=pid, now=now)
    reason = lambda pid, nat=True: job_block_reason(store, cfg, proposal=store.get_proposal(pid), now_dt=NOW, auto=True, is_native=nat)  # noqa: E731
    assert reason("reply") == "external_auto"  # email never runs unattended, even listed in auto_external
    assert reason("deploy") == "external_auto"  # never unattended
    assert reason("comment") is None  # a listed non-email external still runs unattended
    # And email with no native handler (a draft) can never fall back to a free-form agent that could send:
    store.create_task_proposal(proposal_id="draft", task_id="t1", title="Borrador", reason_text="", action_class="external", operation_id="gmail.create_draft",
                               params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="draft", now=now)
    assert job_block_reason(store, cfg, proposal=store.get_proposal("draft"), now_dt=NOW, auto=False, is_native=False) == "no_safe_executor"


def test_final_json_survives_fences_and_stripped_braces():
    body = '"summary": "Hecho", "findings": [], "refs": ["Draft ID: r123"], "proposed_reply": "Hi Bernd", "next_steps": [], "confidence": 0.9, "effects": ["draft"]'
    assert extract_final_json(body)["refs"] == ["Draft ID: r123"]  # host stripped the outer braces
    assert extract_final_json("texto previo\n```json\n{" + body + "}\n```")["proposed_reply"] == "Hi Bernd"
    assert extract_final_json("nada que parsear") is None


def test_job_prompt_carries_the_approved_draft_verbatim():
    from radar.jobs.prompts import build_job_prompt

    task = {"kind": "work_item", "title": "Bug", "summary": "", "checklist": [], "entities": {}}
    proposal = {"title": "Enviar la solicitud", "reason_text": "", "operation_id": "gmail.reply",
                "params": {"draft": "Hi Bernd,\n\nCould you send screenshots?", "draft_ref": "Draft ID: r123"},
                "options": [{"id": "do_it", "label": "Enviar borrador"}], "response": {"actor": "telegram:7", "channel": "telegram"}, "responded_at": "2026-10-19T10:00:00Z"}
    prompt = build_job_prompt(task=task, proposal=proposal, option="do_it", action_class="external", signals=[], facts=[], owner={"language": "es"},
                              workdir=None, grant={"option": "do_it", "label": "Enviar borrador", "at": "2026-10-19T10:00:00Z", "actor": "telegram:7", "channel": "telegram"})
    assert "APPROVED DRAFT" in prompt and "Could you send screenshots?" in prompt and "Draft reference: Draft ID: r123" in prompt
    assert "send that draft" in prompt and "```json" in prompt


def _running_step(store, cfg, now="2026-10-19T10:00:00Z", lease="2026-10-19T10:07:00Z", auto=True):
    try:
        store.get_task("t1")
    except KeyError:
        store.create_task(task_id="t1", kind="work_item", title="Bug", summary="", entities={}, expectations=[], checklist=[], now=now)
        store.create_task_proposal(proposal_id="p1", task_id="t1", title="Investigar", reason_text="", action_class="read", operation_id="investigate",
                                   params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fp", now=now)
        store.respond_proposal("p1", decision="accepted", response={"option": "do", "auto": True}, now=now)
    assert enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("p1"), option="do", now_dt=NOW, auto=auto)
    return store.claim_queued_jobs(now, lease, 1)[0]


def _scheduler(store, cfg, at, alive=()):
    runtime = SimpleNamespace(job_tasks=[SimpleNamespace(get_name=lambda job_id=job_id: f"radar:job:{job_id}") for job_id in alive],
                              spawn_job=lambda job: None)
    return Scheduler(store, cfg, worker_id="w", clock=lambda: at, runtime=runtime)


def test_a_job_whose_lease_runs_out_keeps_it_while_it_runs_and_is_retried_once_when_lost(store):
    cfg = config()
    job = _running_step(store, cfg)
    later = NOW + timedelta(minutes=10)
    asyncio.run(_scheduler(store, cfg, later, alive=[job["id"]])._run_jobs(later))
    assert store.get_job(job["id"])["status"] == "running" and store.get_job(job["id"])["lease_until"] > "2026-10-19T10:10:00Z"
    # The gateway died under it: nobody runs it any more. Its step waits to start again, without a result signal.
    much_later = NOW + timedelta(hours=1)
    assert asyncio.run(_scheduler(store, cfg, much_later)._run_jobs(much_later))["recovered"] == 1
    assert store.get_job(job["id"])["status"] == "cancelled" and store.get_job(job["id"])["error"] == "lease expired"
    assert store.get_proposal("p1")["status"] == "waiting" and store.list_signals(kind="job_result") == []
    # Lost a second time: the step ends unknown and the task re-plans from the result signal.
    store.set_proposal_status("p1", "accepted", "2026-10-19T11:00:00Z")
    again = _running_step(store, cfg, now="2026-10-19T11:00:00Z", lease="2026-10-19T11:07:00Z", auto=False)  # past the auto cooldown
    end = NOW + timedelta(hours=2)
    asyncio.run(_scheduler(store, cfg, end)._run_jobs(end))
    assert store.get_job(again["id"])["status"] == "unknown" and store.get_proposal("p1")["status"] == "unknown"
    assert [s["meta"]["status"] for s in store.list_signals(kind="job_result")] == ["unknown"]


def test_a_step_left_executing_by_a_lost_job_is_released(store):
    cfg = config()
    job = _running_step(store, cfg)
    # Before 2026-10-25 the lost job was closed and its step forgotten, executing for good.
    store.finish_job(job["id"], fencing_token=job["fencing_token"], status="unknown", summary=None, result={}, usage={}, error="lease expired",
                     now="2026-10-19T10:08:00Z")
    assert store.get_proposal("p1")["status"] == "executing"
    assert release_orphans(store, cfg, NOW + timedelta(days=2)) == 1
    proposal = store.get_proposal("p1")
    assert proposal["status"] == "waiting" and proposal["params"]["wait_reason"] == "interrupted"
    assert release_orphans(store, cfg, NOW + timedelta(days=2)) == 0

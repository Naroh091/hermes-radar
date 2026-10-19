from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from conftest import NOW, FakeContext, FakeLlm, active_config, verify_destination

from radar import patterns
from radar.delivery.responses import respond_to_proposal
from radar.jobs.service import enqueue_job_for_proposal, record_job_outcome
from radar.operations import native
from radar.pipeline.process import _settle_draft
from radar.runtime import Runtime
from radar.scheduler import ensure_routines
from radar.sources.cron_output import CronOutputSource, parse_output
from radar.trust import may_auto_run

NOW_S = "2026-10-19T10:00:00Z"


def _signal(kind, at, title="", meta=None, snippet=""):
    return {"id": f"{kind}-{at}", "kind": kind, "occurred_at": at, "title": title, "snippet": snippet, "meta": meta or {}}


def _weekly(start, weeks, hour=9, minute=0, weekday_shift=0):
    base = NOW - timedelta(days=NOW.weekday()) + timedelta(days=weekday_shift)  # Monday of this week
    out = []
    for w in range(weeks):
        at = (base - timedelta(weeks=start + w)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        out.append(at.isoformat().replace("+00:00", "Z"))
    return out


def test_mining_finds_weekly_routines_and_sequences_with_lift():
    signals = []
    for at in _weekly(1, 6, hour=9, weekday_shift=0):  # Mondays 09:00 UTC: owner asks for the weekly report
        signals.append(_signal("session_turn", at, meta={"user_text": "prepárame el resumen semanal de despliegues"}))
    for at in _weekly(1, 5, hour=16, weekday_shift=4):  # Fridays 16:00: PR merged by ftello
        signals.append(_signal("github", at, title="PR merged", meta={"actor": "ftello", "event_type": "pull_request", "action": "closed", "merged": True, "repo": "MalditaEs/DMS"}))
    for at in _weekly(1, 5, hour=11, weekday_shift=7):  # the following Monday 11:00: deployment to production
        signals.append(_signal("github", at, title="Deployment", meta={"actor": "vercel[bot]", "event_type": "deployment_status", "action": "created", "repo": "MalditaEs/DMS", "environment": "production"}))
    signals.append(_signal("email", "2026-10-18T08:00:00Z", title="Re: Factura 1234", meta={"sender": "Billing <billing@example.com>"}))
    found = patterns.mine(signals, now_dt=NOW, tz="Europe/Madrid")
    periodic = {tuple(c["key"]): c for c in found if c["kind"] == "periodic"}
    assert ("session", "owner", "prepárame resumen semanal despliegues") in periodic
    weekly = periodic[("session", "owner", "prepárame resumen semanal despliegues")]
    assert weekly["support"] == 6 and weekly["weekday"] == 0 and weekly["hour"] == 11 and weekly["schedule_cron"] == "0 11 * * 1"  # 09:00 UTC is 11:00 in Madrid
    sequences = [c for c in found if c["kind"] == "sequence"]
    assert any(c["key"][2].startswith("pull_request:merged") and c["then"][2].startswith("deployment_status") for c in sequences)
    seq = next(c for c in sequences if c["key"][2].startswith("pull_request:merged"))
    assert seq["support"] == 5 and seq["lift"] >= 2 and 60 <= seq["median_lag_hours"] <= 70
    assert patterns.event_key(_signal("email", NOW_S, title="Fwd: Factura 5678", meta={"sender": "Billing <billing@example.com>"})) == ("email", "billing@example.com", "factura")
    assert patterns.event_key(_signal("calendar", NOW_S)) is None
    # Too few weeks or too spread out: nothing.
    sparse = [_signal("session_turn", at, meta={"user_text": "mira el correo de bernd"}) for at in _weekly(1, 2)]
    assert patterns.mine(sparse, now_dt=NOW) == []


def test_mining_routine_names_patterns_and_proposes_automations_that_always_ask(store, tmp_path):
    cfg = active_config()
    verify_destination(store)
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=NOW_S)
    for index, at in enumerate(_weekly(1, 6, hour=9)):
        store.add_signal(signal_id=f"s{index}", source_id="src", kind="session_turn", occurred_at=at, external_ref=f"t{index}", group_key=f"g{index}",
                         title="turn", snippet="", meta={"user_text": "prepárame el resumen semanal de despliegues"}, fingerprint=f"f{index}", now=NOW_S, triage_status="extracted")
    answer = {"patterns": [{"fingerprint": "", "name": "Resumen semanal de despliegues", "description": "Cada lunes pides el resumen de despliegues de la semana.",
                            "worth_automating": True, "schedule_cron": "0 11 * * 1", "prompt": "Compila el resumen de despliegues de la semana desde GitHub y envíalo.",
                            "rationale": "Se repite cada lunes", "confidence": 0.8}]}
    candidates = patterns.mine(store.signals_in_window("2026-08-01T00:00:00Z", NOW_S, kinds=("session_turn",)), now_dt=NOW, tz="Europe/Madrid")
    answer["patterns"][0]["fingerprint"] = candidates[0]["fingerprint"]
    llm = FakeLlm({"radar.pattern": [answer]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    outcome = asyncio.run(patterns.run_mining(rt, NOW))
    assert outcome["candidates"] == 1 and outcome["new"] == 1 and outcome["proposed"] == 1
    pattern = store.list_patterns()[0]
    assert pattern["status"] == "proposed" and pattern["name"] == "Resumen semanal de despliegues" and pattern["support"] == 6
    task = store.get_task(pattern["task_id"])
    assert task["kind"] == "routine" and task["title"].startswith("Patrón: Resumen") and task["entities"]["schedule_cron"] == "0 11 * * 1"
    proposal = store.list_proposals(task_id=task["id"])[0]
    assert proposal["operation_id"] == "cron.create" and proposal["action_class"] == "local" and [o["id"] for o in proposal["options"]] == ["automate", "detail", "no_automate"]
    assert may_auto_run({"level": "balanced"}, "local", operation_id="cron.create") is False  # never unattended
    # A second run refreshes evidence without re-asking the model.
    again = asyncio.run(patterns.run_mining(rt, NOW))
    assert again["new"] == 0 and len(store.list_patterns()) == 1 and len(llm.calls) == 1
    # Accepting runs the native cron.create with the destination as origin; the pattern becomes automated.
    store.respond_proposal(proposal["id"], decision="accepted", response={"option": "automate", "actor": "telegram:7", "channel": "telegram"}, now=NOW_S)
    job = enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal(proposal["id"]), option="automate", now_dt=NOW)
    assert job["kind"] == "native"
    spec = json.loads(job["prompt"])
    assert spec["params"]["schedule"] == "0 11 * * 1" and spec["params"]["origin"] == {"platform": "telegram", "chat_id": "42"}

    class Cron:
        def __init__(self):
            self.jobs = {}

        def create(self, *, prompt, schedule, name, origin=None):
            self.jobs["abc123"] = {"id": "abc123", "prompt": prompt, "schedule": schedule, "name": name, "origin": origin}
            return {"id": "abc123", "name": name, "schedule": schedule}

        def get(self, job_id):
            return self.jobs.get(job_id)

    cron = Cron()
    rt.write_provider = lambda kind, account="": cron if kind == "cron" else None
    claimed = store.claim_queued_jobs(NOW_S, "2026-10-19T10:10:00Z", 5)
    result = asyncio.run(native.run_job(rt, claimed[0]))
    assert result.status == "succeeded" and cron.jobs["abc123"]["origin"]["chat_id"] == "42" and result.result["refs"] == ["cron:abc123"]
    record_job_outcome(store, cfg, job=claimed[0], outcome=result, now_dt=NOW)
    _settle_draft(store, task["id"], store.list_signals(kind="job_result", limit=1)[0], NOW_S)
    pattern = store.get_pattern(pattern["id"])
    assert pattern["status"] == "automated" and pattern["automation_ref"] == "abc123" and store.get_task(task["id"])["status"] == "done"


def test_declining_an_automation_dismisses_the_pattern_and_cron_outputs_become_signals(store, tmp_path):
    cfg = active_config()
    verify_destination(store)
    store.upsert_pattern(pattern_id="pat1", kind="periodic", fingerprint="fp1", evidence={"key": ["session", "owner", "x"]}, support=4, confidence=0.7, now=NOW_S)
    store.create_task(task_id="t1", kind="routine", title="Patrón: x", summary="", entities={"pattern_id": "pat1", "schedule_cron": "0 9 * * 1", "prompt": "p"}, expectations=[], checklist=[], now=NOW_S)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Automatizar: x", reason_text="", action_class="local", operation_id="cron.create", params={"native": {"schedule": "0 9 * * 1", "prompt": "p", "name": "x"}},
                               options=[{"id": "automate", "label": "Automatizar"}, {"id": "no_automate", "label": "No automatizar"}], urgency="low", expires_at="2026-10-26T10:00:00Z", fingerprint="fpp1", now=NOW_S)
    store.set_pattern_status("pat1", "proposed", NOW_S, task_id="t1")
    declined = respond_to_proposal(store, cfg, proposal_id="p1", option="no_automate", actor="telegram:7", now_dt=NOW)
    assert declined["outcome"] == "declined" and store.get_pattern("pat1")["status"] == "dismissed" and store.get_task("t1")["status"] == "done"
    # Outputs of an automated pattern's cron job come back as cron_result signals (silent runs are skipped).
    store.upsert_pattern(pattern_id="pat2", kind="periodic", fingerprint="fp2", evidence={}, support=5, confidence=0.9, now=NOW_S)
    store.create_task(task_id="t2", kind="routine", title="Patrón: resumen", summary="", entities={"pattern_id": "pat2"}, expectations=[], checklist=[], now=NOW_S)
    store.set_pattern_status("pat2", "automated", NOW_S, task_id="t2", automation_ref="job42", name="Resumen semanal")
    folder = tmp_path / "output" / "job42"
    folder.mkdir(parents=True)
    (folder / "2026-10-19_09-00-00.md").write_text("# Cron Job: Resumen semanal\n\n**Job ID:** job42\n**Run Time:** 2026-10-19 09:00:00\n\n## Prompt\n\nx\n\n## Response\n\n[SILENT]\n", encoding="utf-8")
    (folder / "2026-10-19_11-00-00.md").write_text("# Cron Job: Resumen semanal\n\n**Job ID:** job42\n**Run Time:** 2026-10-19 11:00:00\n\n## Prompt\n\nx\n\n## Response\n\nEsta semana: 3 despliegues, 1 rollback.\n", encoding="utf-8")
    assert parse_output((folder / "2026-10-19_11-00-00.md").read_text()) == {"name": "Resumen semanal", "run_time": "2026-10-19 11:00:00", "response": "Esta semana: 3 despliegues, 1 rollback."}
    store.add_source(source_id="local:cron", kind="hermes", account_ref="cron", resource_scope={}, retention_days=30, now=NOW_S)
    source = CronOutputSource(store.get_source("local:cron"), cfg, store, output_dir=tmp_path / "output", clock=lambda: NOW)
    result = source.sync(NOW)
    assert result.seen == 2 and result.added == 1 and result.cursor["files"] == {"job42": "2026-10-19_11-00-00.md"}
    signal = store.list_signals(kind="cron_result", limit=1)[0]
    assert signal["group_key"] == "task:t2" and signal["meta"]["pattern_id"] == "pat2" and signal["snippet"].startswith("Esta semana")
    store.update_source_cursor(source.id, result.cursor, NOW_S, success=True)
    source.row = store.get_source(source.id)
    assert source.sync(NOW).added == 0
    # The daily routine is armed once, an hour after install, and keeps its own schedule afterwards.
    ensure_routines(store, NOW_S)
    ensure_routines(store, "2026-10-19T12:00:00Z")
    routine = next(f for f in store.list_followups(limit=20) if f["id"] == "routine:pattern-mining")
    assert routine["next_check_at"] == "2026-10-19T11:00:00Z"

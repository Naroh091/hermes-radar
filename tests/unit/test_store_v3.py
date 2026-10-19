from __future__ import annotations

import sqlite3
import threading

import pytest

from radar.store import Scope, Store

NOW = "2026-10-19T10:00:00Z"
LATER = "2026-10-19T10:30:00Z"


def _source(store: Store) -> str:
    store.add_source(source_id="configured:gmail-a", kind="gmail", account_ref="a@example.org", resource_scope={"accounts": ["a@example.org"]}, retention_days=30, now=NOW)
    return "configured:gmail-a"


def _signal(store: Store, source_id: str, ref: str, *, group: str = "thread:x", next_at: str = NOW, kind: str = "email") -> str:
    signal_id = f"sig-{ref}"
    assert store.add_signal(
        signal_id=signal_id, source_id=source_id, kind=kind, occurred_at=NOW, external_ref=ref, group_key=group,
        title=f"Subject {ref}", snippet="body", meta={"sender": "shop@example.org", "account": "a@example.org"},
        fingerprint=f"fp-{ref}", now=NOW, next_attempt_at=next_at, raw_content="full body", raw_expires_at=LATER,
    )
    return signal_id


def test_fresh_database_reaches_schema_9_and_rebuilt_tables_keep_fk_names(store: Store):
    assert store.status()["schema_version"] == 11
    tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"signals", "tasks", "task_signals", "jobs", "facts", "preferences", "proposals", "plans", "runs"} <= tables
    sql = store.db.execute("SELECT sql FROM sqlite_master WHERE name='consents'").fetchone()[0]
    assert "REFERENCES proposals" in sql
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_signals_dedupe_bundle_claim_and_finish(store: Store):
    source = _source(store)
    _signal(store, source, "m1")
    assert not store.add_signal(signal_id="dup", source_id=source, kind="email", occurred_at=NOW, external_ref="m1", group_key="thread:x",
                                title="", snippet="", meta={}, fingerprint="fp", now=NOW)
    _signal(store, source, "m2")
    _signal(store, source, "m3", group="thread:y", next_at="2026-10-19T11:00:00Z")
    bundles = store.claim_signal_bundles(NOW, LATER, 5)
    assert [b["group_key"] for b in bundles] == ["thread:x"]
    assert [s["external_ref"] for s in bundles[0]["signals"]] == ["m1", "m2"]
    assert store.claim_signal_bundles(NOW, LATER, 5) == []  # leased
    store.finish_signals(bundles[0]["signals"], status="extracted", reason="new", now=NOW, extracts={"sig-m1": {"category": "trip"}})
    row = store.get_signal("sig-m1")
    assert row["triage_status"] == "extracted" and row["extract"] == {"category": "trip"} and row["lease_until"] is None
    assert store.pending_signal_count() == 1
    later = store.claim_signal_bundles("2026-10-19T11:00:00Z", "2026-10-19T11:30:00Z", 5)
    assert later[0]["signals"][0]["external_ref"] == "m3"
    store.defer_signals(later[0]["signals"], next_attempt_at="2026-10-19T12:00:00Z", error="llm down", now=NOW)
    assert store.get_signal("sig-m3")["next_attempt_at"] == "2026-10-19T12:00:00Z"


def test_prune_clears_raw_content_but_keeps_signals_linked_to_open_tasks(store: Store):
    source = _source(store)
    _signal(store, source, "m1")
    store.create_task(task_id="t1", kind="trip", title="Lisboa", summary="viaje", entities={}, expectations=[], checklist=[], now=NOW)
    store.link_task_signal("t1", "sig-m1", "origin", NOW)
    store.update_signal("sig-m1", now=NOW, raw_expires_at=NOW)
    with store.transaction() as db:
        db.execute("UPDATE signals SET expires_at=? WHERE id='sig-m1'", (NOW,))
    counts = store.prune_signals(LATER)
    assert counts["raw_content"] == 1 and counts["signals"] == 0
    assert store.get_signal("sig-m1")["raw_content"] is None


def test_tasks_fts_search_update_and_transitions(store: Store):
    store.create_task(task_id="t1", kind="trip", title="Viaje a Lisboa en noviembre", summary="Vuelo TAP", entities={"places": ["Lisboa"], "identifiers": [{"label": "PNR", "value": "ABC123"}]}, expectations=[], checklist=[], now=NOW)
    store.create_task(task_id="t2", kind="purchase", title="Comprar RAM DDR5", summary="32 GB", entities={}, expectations=[], checklist=[], now=NOW)
    found = store.search_tasks("hotel Lisboa ABC123", limit=5)
    assert [t["id"] for t in found] == ["t1"]
    assert store.search_tasks("???") == []
    updated = store.update_task("t1", now=LATER, summary="Vuelo TAP y hotel", checklist=[{"item": "hotel", "status": "missing", "note": ""}])
    assert updated["version"] == 2 and updated["checklist"][0]["status"] == "missing"
    store.set_task_status("t1", "waiting", now=LATER)
    with pytest.raises(ValueError):
        store.set_task_status("t1", "bogus", now=LATER)
    store.set_task_status("t1", "done", now=LATER, reason="all booked")
    assert store.get_task("t1")["closed_reason"] == "all booked"
    assert [t["id"] for t in store.list_tasks(statuses=("done",))] == ["t1"]
    assert store.search_tasks("Lisboa", open_only=True) == []


def test_proposals_dedupe_on_open_fingerprint_and_never_reopen_declined(store: Store):
    store.create_task(task_id="t1", kind="trip", title="Lisboa", summary="", entities={}, expectations=[], checklist=[], now=NOW)
    first = store.create_task_proposal(proposal_id="p1", task_id="t1", title="Buscar hotel", reason_text="falta", action_class="read",
                                       operation_id="web.read", params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z",
                                       fingerprint="fp-hotel", now=NOW, batch_key="now")
    again = store.create_task_proposal(proposal_id="p2", task_id="t1", title="Buscar hotel", reason_text="", action_class="read",
                                       operation_id=None, params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z",
                                       fingerprint="fp-hotel", now=NOW)
    assert first["created"] and not again["created"] and again["id"] == "p1"
    assert store.mark_proposals_sent(["p1"], "outbox-1", NOW) == 1
    store.respond_proposal("p1", decision="declined", response={"option": "no"}, now=LATER)
    suppressed = store.create_task_proposal(proposal_id="p3", task_id="t1", title="Buscar hotel", reason_text="", action_class="read",
                                            operation_id=None, params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z",
                                            fingerprint="fp-hotel", now=LATER)
    assert suppressed.get("suppressed") is True
    with pytest.raises(Exception):
        store.respond_proposal("p1", decision="accepted", response={}, now=LATER)
    store.create_task_proposal(proposal_id="p4", task_id="t1", title="Check-in", reason_text="", action_class="local", operation_id=None,
                               params={}, options=[], urgency="high", expires_at="2026-10-19T10:10:00Z", fingerprint="fp-ci", now=NOW)
    assert store.expire_proposals("2026-10-19T10:20:00Z") == 1


def test_snoozed_proposals_wake_and_jobs_lifecycle(store: Store):
    store.create_task(task_id="t1", kind="work_item", title="Bug 42", summary="", entities={"repo": "org/app"}, expectations=[], checklist=[], now=NOW)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Investigar", reason_text="", action_class="read", operation_id="investigate",
                               params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fp-inv", now=NOW)
    store.respond_proposal("p1", decision="snoozed", response={}, now=NOW, snoozed_until="2026-10-19T12:00:00Z")
    assert store.wake_snoozed_proposals("2026-10-19T11:00:00Z") == 0
    assert store.wake_snoozed_proposals("2026-10-19T12:00:00Z") == 1
    assert store.get_proposal("p1")["status"] == "pending"
    store.create_job(job_id="j1", task_id="t1", proposal_id="p1", kind="proposal", prompt="investigate", action_class="read", now=NOW, workdir="/tmp/app")
    claimed = store.claim_queued_jobs(NOW, LATER, 2)
    assert [j["id"] for j in claimed] == ["j1"] and claimed[0]["fencing_token"] == 1
    assert store.claim_queued_jobs(NOW, LATER, 2) == []
    store.finish_job("j1", fencing_token=1, status="succeeded", summary="root cause found", result={"findings": ["x"]}, usage={}, error=None, now=LATER)
    job = store.get_job("j1")
    assert job["status"] == "succeeded" and job["result"] == {"findings": ["x"]}
    with pytest.raises(RuntimeError):
        store.finish_job("j1", fencing_token=1, status="failed", summary=None, result={}, usage={}, error="x", now=LATER)
    store.create_job(job_id="j2", task_id="t1", proposal_id=None, kind="proposal", prompt="again", action_class="read", now=NOW)
    store.claim_queued_jobs(NOW, "2026-10-19T10:05:00Z", 2)
    assert store.recover_stale_jobs("2026-10-19T10:10:00Z") == 1
    assert store.get_job("j2")["status"] == "unknown"


def test_facts_keep_highest_confidence_and_preferences_are_versioned(store: Store):
    store.upsert_fact(key="Shoe Size", value="42", confidence=0.9, provenance={"signal_id": "s1"}, now=NOW)
    store.upsert_fact(key="shoe size", value="48", confidence=0.3, provenance={"signal_id": "s2"}, now=LATER)
    facts = store.list_facts()
    assert facts == [{"key": "shoe size", "value": "42", "confidence": 0.9, "provenance": {"signal_id": "s1"}, "updated_at": LATER}]
    pref = {"id": "silence-newsletters", "dimension": "notification", "scope": {"sender": "news@example.org"}, "effect": "silent",
            "exceptions": [], "instruction": "no me avises de newsletters", "origin": "explicit", "status": "active"}
    assert store.save_preference(pref, expected_version=0, now=NOW) == 1
    with pytest.raises(ValueError):
        store.save_preference(pref, expected_version=0, now=NOW)
    assert store.save_preference({**pref, "status": "disabled"}, expected_version=1, now=LATER) == 2
    assert store.list_preferences(active_only=True) == []
    assert [p["version"] for p in store.preference_history("silence-newsletters")] == [1, 2]
    with pytest.raises(ValueError):
        store.save_preference({**pref, "id": "inferred", "origin": "inferred"}, expected_version=0, now=NOW)


def test_store_is_safe_across_threads(store: Store):
    source = _source(store)
    errors: list[Exception] = []

    def writer(prefix: str) -> None:
        try:
            for index in range(20):
                _signal(store, source, f"{prefix}{index}", group=f"g{prefix}")
        except Exception as exc:  # pragma: no cover - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("a", "b", "c")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert store.pending_signal_count() == 60
    assert len(store._connections) >= 2


def test_scope_isolation_covers_new_tables(tmp_path):
    first = Store(tmp_path / "db.sqlite3", Scope("profile-a", "owner-a"))
    second = Store(tmp_path / "db.sqlite3", Scope("profile-a", "owner-b"))
    first.create_task(task_id="t1", kind="other", title="only a", summary="", entities={}, expectations=[], checklist=[], now=NOW)
    first.upsert_fact(key="k", value="v", confidence=0.5, provenance={}, now=NOW)
    assert second.list_tasks() == [] and second.list_facts() == [] and second.search_tasks("only") == []
    assert second.status()["counts"]["tasks"] == 0 and first.status()["counts"]["tasks"] == 1
    first.close()
    second.close()

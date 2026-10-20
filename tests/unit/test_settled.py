"""A settled task asks nothing more, a decision with no step still reaches the owner, and pull requests are re-checked before the summary."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from conftest import NOW, FakeContext, FakeLlm, active_config, extract_answer, plan_answer, verify_destination

from radar.delivery.dispatch import dispatch
from radar.delivery.notice import build_digest, build_notice
from radar.pipeline import act, extract as extract_step, reason
from radar.pipeline.process import process_bundle
from radar.pipeline.refresh import SOURCE_ID, pr_reference, refresh_before_digest
from radar.runtime import Runtime

NOW_S = "2026-10-19T10:00:00Z"


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _apply(store, cfg, plan, *, extract=None, existing_task=None):
    return act.apply(store, cfg, signals=[], extract=extract_step.validate_extract(extract or extract_answer()),
                     plan=reason.validate_plan(plan), now_dt=NOW, preferences=[], existing_task=existing_task)


def _proposal(store, pid, task_id, title, operation, *, batch_key="digest", deliver_after=""):
    store.create_task_proposal(proposal_id=pid, task_id=task_id, title=title, reason_text="", action_class="read", operation_id=operation,
                               params={"deliver_after": deliver_after}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z",
                               fingerprint=pid, now=NOW_S, batch_key=batch_key)
    return store.get_proposal(pid)


def _task(store, task_id, title, *, kind="work_item", entities=None, status=None):
    store.create_task(task_id=task_id, kind=kind, title=title, summary="", entities=entities or {}, expectations=[], checklist=[], now=NOW_S)
    if status:
        store.set_task_status(task_id, status, now=NOW_S)
    return store.get_task(task_id)


def test_a_settled_task_withdraws_its_open_asks_and_keeps_only_news(store):
    cfg = active_config()
    verify_destination(store)
    _task(store, "t1", "Revisar el PR #88 de core-api", status="needs_user")
    _proposal(store, "p-ask", "t1", "Preparar el comentario de revisión", None)
    _proposal(store, "p-sent", "t1", "Pedir una revisión más profunda", "investigate", batch_key="now")
    store.mark_proposals_sent(["p-sent"], "out-1", NOW_S)
    store.set_task_status("t1", "done", now=NOW_S, reason="merged")
    assert {p["id"]: p["status"] for p in store.list_proposals(task_id="t1")} == {"p-ask": "expired", "p-sent": "expired"}
    # News written after the closure still goes out...
    _proposal(store, "p-news", "t1", "El PR #88 ya está mergeado", "inform", batch_key="now")
    assert dispatch(store, cfg, NOW)["notices"] == 1 and store.get_proposal("p-news")["status"] == "sent"
    # ...while a step that somehow survives on a closed task is never delivered.
    _proposal(store, "p-stale", "t1", "Comentar en GitHub", "github.comment", batch_key="now")
    assert dispatch(store, cfg, NOW)["notices"] == 0 and store.get_proposal("p-stale")["status"] == "pending"


def test_a_decision_with_no_step_still_reaches_the_owner(store):
    cfg = active_config()
    verify_destination(store)
    quiet = plan_answer(task={**plan_answer()["task"], "kind": "purchase", "title": "Decidir la valoración del pedido", "status": "needs_user"},
                        proposals=[], checks=[], escalation={"decision": "ask", "why": "only the owner can rate"},
                        message="G2G pondrá una valoración positiva por defecto el 22 de octubre si no eliges una. ¿Positiva, neutra o negativa?",
                        notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"})
    result = _apply(store, cfg, quiet, extract=extract_answer(category="purchase"))
    [item] = store.list_proposals(task_id=result.task_id)
    assert item["operation_id"] == "inform" and item["params"]["ask"] is True and item["batch_key"] == "digest"
    assert item["params"]["deliver_after"] == "2026-10-20T07:00:00Z"  # the next morning summary, 09:00 local
    payload = build_notice(store.get_task(result.task_id), [item], language="es")
    assert "¿Positiva, neutra o negativa?" in payload["text"] and "Responde a este mensaje" in payload["text"]
    assert all("Vale" not in button["label"] for row in payload["buttons"] for button in row)
    assert payload["buttons"][-1][0]["data"] == f"pt:{result.task_id}:mute"
    digest = build_digest([(store.get_task(result.task_id), [item])], language="es")
    assert digest["proposal_ids"] == [item["id"]] and digest["buttons"] == []
    # A security alert only the owner can act on interrupts right away, with no step to offer.
    urgent = plan_answer(task={**plan_answer()["task"], "kind": "check", "title": "Dos secretos detectados en hermes-radar", "status": "needs_user"},
                         proposals=[], checks=[], escalation={"decision": "ask", "why": "credentials may be live"},
                         message="GitGuardian detectó dos secretos en el commit de esta mañana. Conviene rotarlos.",
                         notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"})
    alert = _apply(store, cfg, urgent, extract=extract_answer(category="check", risk_flags=["security"]))
    [item] = store.list_proposals(task_id=alert.task_id)
    assert item["batch_key"] == "now" and item["urgency"] == "high" and item["params"]["ask"] is True
    assert dispatch(store, cfg, NOW)["notices"] == 1


def test_a_merge_turns_the_pending_ask_into_one_line_of_news(store):
    cfg = active_config()
    verify_destination(store)
    task = _task(store, "t2", "Revisar el PR #88 de core-api", entities={"repo": "org/core-api", "url": "https://github.com/org/core-api/pull/88"}, status="needs_user")
    _proposal(store, "p-comment", "t2", "Preparar el comentario de revisión", None, deliver_after="2026-10-20T07:00:00Z")
    settled = plan_answer(task_match={"decision": "completes", "task_id": "t2", "why": "merged"},
                          task={"kind": "work_item", "title": "Revisar el PR #88 de core-api", "summary": "Mergeado.", "status": "done", "due_at": "", "next_review_at": "", "confidence": 0.9},
                          proposals=[], checks=[], escalation={"decision": "inform", "why": "settled"},
                          message="El PR #88 de core-api ya está mergeado; no hace falta tu comentario.",
                          notify={"disposition": "digest", "urgency": "whenever", "importance": "low", "reason": "x"})
    result = _apply(store, cfg, settled, existing_task=task)
    assert result.decision == "completes" and store.get_task("t2")["status"] == "done"
    assert store.get_proposal("p-comment")["status"] == "expired"
    [news] = [p for p in store.list_proposals(task_id="t2") if p["operation_id"] == "inform"]
    assert news["status"] == "pending" and news["batch_key"] == "digest" and news["params"]["deliver_after"] == "2026-10-20T07:00:00Z"
    assert "ask" not in news["params"]


class FakeRepo:
    def __init__(self, answer):
        self.answer = answer
        self.calls: list[tuple[str, int]] = []

    def pull_request(self, repo, number):
        self.calls.append((repo, number))
        return self.answer


def test_pull_requests_are_rechecked_shortly_before_their_summary(store, tmp_path):
    cfg = active_config()
    verify_destination(store)
    _task(store, "t3", "Revisar el PR #87 de core-api", entities={"repo": "org/core-api", "url": "https://github.com/org/core-api/pull/87"}, status="needs_user")
    _proposal(store, "p-draft", "t3", "Preparar comentario de revisión", "gmail.create_draft", deliver_after="2026-10-20T07:00:00Z")
    _task(store, "t4", "Confirmar la llamada de la oficina", kind="commitment", status="needs_user")
    _proposal(store, "p-call", "t4", "Decidir si asistir", None, deliver_after="2026-10-20T07:00:00Z")
    assert pr_reference(store.get_task("t3")) == ("org/core-api", 87) and pr_reference(store.get_task("t4")) is None
    assert pr_reference({"title": "Revisar el PR #12 de dms", "entities": {"repo": "org/dms"}}) == ("org/dms", 12)
    gh = FakeRepo({"state": "closed", "merged": True, "merged_at": "2026-10-20T06:10:00Z", "title": "feat(search): research",
                   "html_url": "https://github.com/org/core-api/pull/87", "merged_by": {"login": "james"}})
    early = datetime(2026, 10, 20, 6, 30, tzinfo=timezone.utc)  # 08:30 local: the 09:00 summary is not close yet
    assert refresh_before_digest(store, cfg, early, provider=gh) == {"checked": 0, "changed": 0} and gh.calls == []
    close = datetime(2026, 10, 20, 6, 45, tzinfo=timezone.utc)  # 08:45 local
    assert refresh_before_digest(store, cfg, close, provider=gh) == {"checked": 1, "changed": 1} and gh.calls == [("org/core-api", 87)]
    [signal] = store.list_signals(kind="github")
    assert signal["source_id"] == SOURCE_ID and signal["meta"]["task_id"] == "t3" and signal["meta"]["merged"] is True
    assert signal["title"] == "PR #87 merged: feat(search): research" and signal["triage_status"] == "pending"
    # Not asked again for hours, and the same outcome never becomes a second signal.
    assert refresh_before_digest(store, cfg, close + timedelta(minutes=1), provider=gh)["checked"] == 0
    # The pipeline binds the signal to its task with no extraction call; the planner settles it and the ask is withdrawn.
    llm = FakeLlm({"radar.plan": [plan_answer(
        task_match={"decision": "completes", "task_id": "t3", "why": "merged"},
        task={"kind": "work_item", "title": "Revisar el PR #87 de core-api", "summary": "Mergeado.", "status": "done", "due_at": "", "next_review_at": "", "confidence": 0.9},
        proposals=[], checks=[], escalation={"decision": "inform", "why": "settled"}, message="El PR #87 ya está mergeado.",
        notify={"disposition": "digest", "urgency": "whenever", "importance": "low", "reason": "x"})]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: close, hermes_home=str(tmp_path))
    [bundle] = store.claim_signal_bundles(iso(close), iso(close + timedelta(minutes=5)), 5)
    outcome = asyncio.run(process_bundle(rt, bundle, close))
    assert outcome["decision"] == "completes" and store.get_task("t3")["status"] == "done"
    assert store.get_proposal("p-draft")["status"] == "expired" and store.get_proposal("p-call")["status"] == "pending"
    assert [call["purpose"] for call in llm.calls] == ["radar.plan"]
    assert [p["operation_id"] for p in store.list_proposals(task_id="t3", statuses=("pending",))] == ["inform"]


def _runtime(store, cfg, tmp_path, clock=lambda: NOW):
    return Runtime(FakeContext(cfg), store, cfg, worker_id="w", clock=clock, hermes_home=str(tmp_path))


def test_a_step_for_a_chosen_moment_is_its_own_notice_at_that_moment(store):
    cfg = active_config()
    verify_destination(store)
    opening = "2026-10-22T05:45:00Z"  # check-in opens 24h before a 07:45 local departure
    plan = plan_answer(task={**plan_answer()["task"], "title": "Viaje Madrid-Valencia, 23–25 de octubre"},
                       proposals=[{"title": "Facturar el vuelo IB8832", "rationale": "La facturación abre 24 h antes.", "action_class": "external",
                                   "operation": "web.act", "when": opening, "urgency": "normal", "options": [{"id": "do", "label": "Facturar"}, {"id": "no", "label": "No"}]}],
                       checks=[], notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"})
    result = _apply(store, cfg, plan)
    [step] = store.list_proposals(task_id=result.task_id)
    assert step["batch_key"] == "now" and step["params"]["deliver_after"] == opening and step["params"]["when"] == opening
    assert dispatch(store, cfg, NOW) == {"notices": 0, "digests": 0, "proposals": 0}  # nothing before its moment
    assert dispatch(store, cfg, datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc))["digests"] == 0  # not summary filler either
    counts = dispatch(store, cfg, datetime(2026, 10, 22, 5, 45, tzinfo=timezone.utc))
    assert counts["notices"] == 1
    [row] = store.list_outbox(limit=5)
    assert "Facturar el vuelo IB8832" in row["payload"]["text"] and row["payload"]["buttons"][0][0]["label"] == "Facturar"


def test_summary_slots_no_longer_hold_back_automatic_steps(store, tmp_path):
    from radar.pipeline.process import _start_auto, retry_pending_jobs

    cfg = active_config(trust={"level": "autonomous", "overrides": {}, "auto_external": []})
    verify_destination(store)
    plan = plan_answer(task={**plan_answer()["task"], "kind": "work_item", "title": "Revisar el PR #87"},
                       proposals=[{"title": "Preparar comentario de revisión", "rationale": "", "action_class": "local", "operation": "gmail.create_draft",
                                   "when": "", "urgency": "normal", "options": []}],
                       checks=[], notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"})
    result = _apply(store, cfg, plan, extract=extract_answer(category="work_item"))
    [draft] = store.list_proposals(task_id=result.task_id)
    assert draft["batch_key"] == "digest" and draft["params"]["deliver_after"] == "2026-10-20T07:00:00Z"
    rt = _runtime(store, cfg, tmp_path)
    assert _start_auto(rt, draft, store.get_task(result.task_id), NOW) is True  # the slot delays the news, not the work
    assert store.get_proposal(draft["id"])["status"] in ("accepted", "waiting", "executing") and store.list_jobs(task_id=result.task_id, limit=5)
    # A step the planner set for later waits for its moment, then runs by itself when trust allows...
    later = plan_answer(task={**plan_answer()["task"], "title": "Informe mensual"},
                        proposals=[{"title": "Preparar el informe", "rationale": "", "action_class": "local", "operation": "",
                                    "when": "2026-10-19T12:00:00Z", "urgency": "normal", "options": []}],
                        checks=[], notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"})
    scheduled = _apply(store, cfg, later)
    [report] = store.list_proposals(task_id=scheduled.task_id)
    assert _start_auto(rt, report, store.get_task(scheduled.task_id), NOW) is False
    assert retry_pending_jobs(rt, NOW) == 0
    assert retry_pending_jobs(rt, datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)) == 1
    assert store.get_proposal(report["id"])["status"] in ("accepted", "waiting", "executing")
    # ...while an external one still reaches the owner as a notice at its moment.
    external = plan_answer(task={**plan_answer()["task"], "title": "Tren a Gijón"},
                           proposals=[{"title": "Facturar el tren", "rationale": "", "action_class": "external", "operation": "web.act",
                                       "when": "2026-10-19T12:00:00Z", "urgency": "normal", "options": []}],
                           checks=[], notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"})
    asked = _apply(store, cfg, external)
    at_noon = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    assert retry_pending_jobs(rt, at_noon) == 0
    [checkin] = store.list_proposals(task_id=asked.task_id)
    assert checkin["status"] == "pending" and dispatch(store, cfg, at_noon)["notices"] == 1

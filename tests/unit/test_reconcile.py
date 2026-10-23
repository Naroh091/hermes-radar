"""One open step per family and task: a re-plan updates the step it restates, withdraws the rest, never duplicates running work."""
from __future__ import annotations

from datetime import timedelta

from conftest import NOW, config, extract_answer, plan_answer

from radar.pipeline import act, correlate, extract as extract_step, reason
from radar.pipeline.process import task_context
from radar.pipeline.reconcile import dedupe_open, family, same_family

NOW_S = "2026-10-19T10:00:00Z"


def _step(title, *, operation="", action_class="read", options=None, fact=None, when=""):
    item = {"title": title, "rationale": "", "action_class": action_class, "operation": operation, "when": when, "urgency": "normal",
            "options": options or ([{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}] if operation == "ask" else [])}
    if fact:
        item["fact"] = fact
    return item


def _apply(store, cfg, plan, *, existing_task=None, at=NOW):
    return act.apply(store, cfg, signals=[], extract=extract_step.validate_extract(extract_answer(category="work_item")),
                     plan=reason.validate_plan(plan), now_dt=at, preferences=[], existing_task=existing_task)


def test_families_recognise_the_same_step_under_another_wording():
    pairs = [
        ("Enviar a Arturo el borrador ya preparado", "Enviar a Arturo el borrador preparado", "external", "gmail.reply"),
        ("Publicar comentario de revisión en el PR #198", "Enviar comentario de revisión del PR #198", "external", "gmail.reply"),
        ("Comprobar publicación y despliegue v1.0.6", "Comprobar publicación v1.0.6 y despliegue", "read", "investigate"),
    ]
    for one, other, action_class, operation in pairs:
        assert same_family(family({"title": one, "action_class": action_class, "operation": operation}),
                           family({"title": other, "action_class": action_class, "operation_id": operation})), (one, other)
    assert not same_family(family({"title": "Revisar el estado actual del PR #122", "operation": "web.read"}),
                           family({"title": "Verificar despliegue de master tras PR #121", "operation": "investigate"}))
    # Two questions about the same fact are the same question; about different facts, two questions.
    same_fact = {"key": "gestion_iberifier_eu", "yes": "nosotros", "no": "externa"}
    assert same_family(family({"title": "¿La web de Iberifier la llevamos nosotros?", "operation": "ask", "fact": same_fact}),
                       family({"title": "¿Gestionamos el servidor de iberifier.eu?", "operation": "ask", "params": {"fact": same_fact}}))
    assert not same_family(family({"title": "¿Rotaste ya la clave de Postgres?", "operation": "ask", "fact": {"key": "postgres_rotated"}}),
                           family({"title": "¿Verificaste el COPY de pgbackup?", "operation": "ask", "fact": {"key": "pgbackup_copy_legit"}}))
    # A step and a question never collapse into each other.
    assert not same_family(family({"title": "Enviar el borrador a Arturo", "action_class": "external"}),
                           family({"title": "¿Enviamos el borrador a Arturo?", "operation": "ask"}))


def test_a_replan_updates_the_restated_step_in_place_withdraws_the_rest_and_leaves_running_work_alone(store):
    cfg = config()
    send = _step("Enviar a Arturo el borrador ya preparado", operation="gmail.reply", action_class="external",
                 options=[{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}])
    verify = _step("Verificar cláusulas del adjunto original", operation="investigate")
    first = _apply(store, cfg, plan_answer(task={**plan_answer()["task"], "kind": "work_item", "title": "Acuerdo IES"}, proposals=[send, verify],
                                           notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}))
    rows = {p["title"]: p for p in store.list_proposals(task_id=first.task_id)}
    sent_id = rows["Enviar a Arturo el borrador ya preparado"]["id"]
    store.mark_proposals_sent([sent_id], "outbox-1", NOW_S)  # the owner has it on screen as «1»
    running = rows["Verificar cláusulas del adjunto original"]
    store.respond_proposal(running["id"], decision="accepted", response={"auto": True}, now=NOW_S)
    store.set_proposal_status(running["id"], "waiting", NOW_S)
    # Something else the plan will not restate.
    store.create_task_proposal(proposal_id="stale", task_id=first.task_id, title="Preparar aviso para Ramón", reason_text="", action_class="local",
                               operation_id="gmail.create_draft", params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z",
                               fingerprint="stale", now=NOW_S, batch_key="digest")
    # A scheduled step keeps its moment whatever a re-plan says.
    store.create_task_proposal(proposal_id="later", task_id=first.task_id, title="Facturar el vuelo", reason_text="", action_class="local",
                               operation_id="web.act", params={"when": "2026-11-02T06:00:00Z"}, options=[], urgency="normal", expires_at="2026-11-03T10:00:00Z",
                               fingerprint="later", now=NOW_S, batch_key="now")
    task = task_context(store, store.get_task(first.task_id))
    assert {s["id"] for s in task["open_steps"]} == {sent_id, running["id"], "stale", "later"}
    replan = plan_answer(task_match={"decision": "advances", "task_id": first.task_id, "why": "reply"},
                         task={**plan_answer()["task"], "kind": "work_item", "title": "Acuerdo IES"},
                         proposals=[_step("Enviar a Arturo el borrador preparado", operation="gmail.reply", action_class="external",
                                          options=[{"id": "send", "label": "Enviar ahora"}, {"id": "no", "label": "No"}]),
                                    _step("Verificar las cláusulas del adjunto", operation="investigate")],
                         notify={"disposition": "digest", "urgency": "today", "importance": "normal", "reason": "x"})
    again = _apply(store, cfg, replan, existing_task=task, at=NOW + timedelta(hours=1))
    after = {p["id"]: p for p in store.list_proposals(task_id=first.task_id)}
    updated = after[sent_id]
    assert updated["status"] == "sent" and updated["title"] == "Enviar a Arturo el borrador preparado" and updated["options"][0]["label"] == "Enviar ahora"
    assert updated["batch_key"] == "now"  # already on the owner's screen: its number still works
    assert after[running["id"]]["status"] == "waiting"  # running work is neither duplicated nor touched
    assert after["stale"]["status"] == "expired" and after["stale"]["params"]["superseded_by"] == "replan"
    assert after["later"]["status"] == "pending"  # scheduled for its moment, not withdrawn implicitly
    assert len(after) == 4 and again.proposals == []  # nothing new was inserted
    audit = [row for row in store.list_audit(limit=10) if row["decision_code"] == "advances"][-1]["metadata_redacted"]
    assert audit["updated"] == 1 and audit["superseded"] == 1


def test_the_planner_sees_the_open_steps_on_every_path(store):
    cfg = config()
    first = _apply(store, cfg, plan_answer(task={**plan_answer()["task"], "kind": "work_item", "title": "Caída de la web"},
                                           proposals=[_step("¿La web de Iberifier la llevamos nosotros?", operation="ask",
                                                            fact={"key": "gestion_iberifier", "yes": "nosotros", "no": "externa"})],
                                           notify={"disposition": "digest", "urgency": "today", "importance": "normal", "reason": "x"}))
    [ask] = store.list_proposals(task_id=first.task_id)
    assert ask["params"]["fact"] == {"key": "gestion_iberifier", "yes": "nosotros", "no": "externa"}  # the answer can record the fact
    # The thread path: candidates carry their open steps.
    candidates = correlate.candidate_tasks(store, {"category": "work_item", "title": "Caída", "summary": "", "entities": {}})
    assert candidates[0]["id"] == first.task_id and candidates[0]["open_steps"][0]["id"] == ask["id"]
    # The bound path: the planner input names them and the newest proposals come first.
    for index in range(12):
        store.create_task_proposal(proposal_id=f"old{index}", task_id=first.task_id, title=f"Paso {index}", reason_text="", action_class="read", operation_id="investigate",
                                   params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint=f"old{index}",
                                   now=f"2026-10-19T0{index % 9}:00:00Z", batch_key="auto")
    context = task_context(store, store.get_task(first.task_id))
    assert context["recent_proposals"][0]["title"] == ask["title"] and len(context["recent_proposals"]) == 8
    text = reason.build_input(now=NOW_S, owner={}, trust_level="balanced", extract=None, signals=[], candidates=candidates, facts=[], preferences=[], existing_task=context)
    assert '"open_steps"' in text and ask["id"] in text and "unanswered_by_owner" in text
    assert "open_steps" in reason.PLAN_PROMPT and "updated in place" in reason.PLAN_PROMPT


def test_dedupe_keeps_the_newest_of_each_family(store):
    store.create_task(task_id="t1", kind="work_item", title="Workspace", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    titles = [("a", "Enviar respuesta sobre rrs@iberifier.eu", "2026-10-22T13:05:00Z"), ("b", "Enviar borrador a Anais", "2026-10-22T13:10:00Z"),
              ("c", "Enviar respuesta a Anais sobre Workspace", "2026-10-22T13:29:00Z"), ("d", "Enviar aviso a Anais y Ramón sobre Workspace", "2026-10-22T14:41:00Z")]
    for pid, title, at in titles:
        store.create_task_proposal(proposal_id=pid, task_id="t1", title=title, reason_text="", action_class="external", operation_id="gmail.reply",
                                   params={}, options=[], urgency="normal", expires_at="2026-10-25T10:00:00Z", fingerprint=pid, now=at, batch_key="digest")
    store.create_task_proposal(proposal_id="q", task_id="t1", title="¿Eliminamos tu tarjeta de facturación?", reason_text="", action_class="read", operation_id="ask",
                               params={}, options=[{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}], urgency="normal", expires_at="2026-10-25T10:00:00Z",
                               fingerprint="q", now="2026-10-22T14:44:00Z", batch_key="digest")
    withdrawn = dedupe_open(store, "t1", "2026-10-23T12:00:00Z")
    statuses = {p["id"]: p["status"] for p in store.list_proposals(task_id="t1")}
    assert statuses["q"] == "pending" and statuses["d"] == "pending"
    assert withdrawn == 3 and statuses["c"] == "expired" and store.get_proposal("c")["params"]["superseded_by"] == "dedupe"
    assert {statuses[pid] for pid in ("a", "b")} == {"expired"}  # four replies to the same people are one decision, the newest

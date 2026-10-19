from __future__ import annotations

import asyncio
import json

from conftest import NOW, FakeContext, FakeLlm, active_config

from radar.render import cards
from radar.runtime import Runtime

NOW_S = "2026-10-19T10:00:00Z"


def test_builtin_kinds_and_artifacts_draw_real_fields():
    trip = {"kind": "trip", "title": "Viaje a Lisboa", "due_at": "2026-11-03T06:10:00Z",
            "entities": {"places": ["Madrid", "Lisboa"], "dates": [{"label": "ida", "when": "2026-11-03T06:10:00Z"}, {"label": "vuelta", "when": "2026-11-05T19:00:00Z"}],
                         "identifiers": [{"label": "Localizador", "value": "ABC123"}]}}
    kind = cards.assign(trip, [])
    assert kind["id"] == "trip" and kind["layout"] == "boarding-pass"
    art = cards.artifact(trip, kind, language="es", tz="Europe/Madrid", now_dt=NOW)
    assert art["from"] == "Madrid" and art["to"] == "Lisboa" and art["code"] == "ABC123"
    assert art["depart"]["day"] == 3 and art["depart"]["month"] == "nov" and art["depart"]["time"] == "07:10" and art["return"]["day"] == 5
    purchase = {"kind": "purchase", "title": "RAM DDR5", "entities": {"items": ["Kingston Fury 32 GB"], "amounts": [{"label": "total", "value": 129.9, "currency": "EUR"}],
                "identifiers": [{"label": "Pedido", "value": "AMZ-77"}], "organizations": ["Amazon"]}}
    art = cards.artifact(purchase, cards.assign(purchase, []), language="es", tz="UTC", now_dt=NOW)
    assert art["layout"] == "receipt" and art["amount"] == {"value": 129.9, "currency": "EUR", "label": "total"} and art["order"] == "AMZ-77" and art["vendor"] == "Amazon"
    work = {"kind": "work_item", "title": "Fix upload", "entities": {"repo": "MalditaEs/DMS", "branch": {"name": "radar/upload-limit"}, "pr": "https://github.com/MalditaEs/DMS/pull/2010", "draft": {"text": "hi"}}}
    art = cards.artifact(work, cards.assign(work, []), language="en", tz="UTC", now_dt=NOW)
    assert art["layout"] == "ticket" and art["number"] == "#2010" and art["branch"] == "radar/upload-limit" and art["draft"] is True
    commitment = {"kind": "commitment", "title": "Entregar informe", "due_at": "2026-10-22T09:00:00Z", "entities": {"people": ["Ana"]}}
    art = cards.artifact(commitment, cards.assign(commitment, []), language="es", tz="Europe/Madrid", now_dt=NOW)
    assert art["layout"] == "calendar" and art["due"]["day"] == 22 and art["due"]["weekday"] == "jueves" and art["days_left"] == 2 and art["who"] == ["Ana"]
    routine = {"kind": "routine", "title": "Patrón: resumen", "status": "done", "entities": {"schedule_cron": "0 11 * * 1", "pattern_id": "p"}}
    art = cards.artifact(routine, cards.assign(routine, []), language="es", tz="UTC", now_dt=NOW)
    assert art["layout"] == "dial" and art["schedule_label"] == "cada lunes a las 11:00" and art["automated"] is True
    assert cards.cron_label("30 8 * * *", "en") == "every day at 08:30"


def test_other_tasks_cluster_and_a_generated_kind_is_designed_and_assigned(store, tmp_path):
    cfg = active_config()
    for index in range(3):
        store.create_task(task_id=f"inv{index}", kind="other", title=f"Factura {100 + index} de Hetzner pendiente", summary="", entities={"organizations": ["Hetzner"], "amounts": [{"label": "importe", "value": 40 + index, "currency": "EUR"}]},
                          expectations=[], checklist=[], now=NOW_S)
    store.create_task(task_id="odd", kind="other", title="Renovar el pasaporte", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    found = cards.clusters(store.list_tasks(limit=50))
    assert len(found) == 1 and found[0]["size"] == 3 and "factura" in found[0]["cluster"] and "hetzner" in found[0]["cluster"]
    answer = {"kinds": [{"cluster": found[0]["cluster"], "worth_it": True, "id": "invoice", "label_es": "Factura", "label_en": "Invoice", "icon": "🧾", "hue": 95,
                         "layout": "receipt", "eyebrow_field": "organizations", "headline_field": "amounts", "keywords": ["factura", "invoice", "hetzner", "importe"], "rationale": "Recurring invoices"},
                        {"cluster": "x", "worth_it": False, "id": "nope", "label_es": "", "label_en": "", "icon": "", "hue": 0, "layout": "note", "eyebrow_field": "", "headline_field": "", "keywords": [], "rationale": ""}]}
    llm = FakeLlm({"radar.card": [answer]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    result = asyncio.run(cards.design_card_kinds(rt, NOW))
    assert result == {"clusters": 1, "new": 1, "designed": 1}
    generated = [k for k in store.list_card_kinds(active_only=True) if k["origin"] == "generated"]
    assert generated[0]["id"] == "invoice" and generated[0]["hue"] == 95 and generated[0]["spec"]["keywords"][:2] == ["factura", "invoice"] and generated[0]["support"] == 3
    kind = cards.assign(store.get_task("inv1"), generated)
    assert kind["id"] == "invoice" and kind["layout"] == "receipt"
    art = cards.artifact(store.get_task("inv1"), kind, language="es", tz="UTC", now_dt=NOW)
    assert art["eyebrow"] == "Hetzner" and art["headline"] == "41" and art["amount"]["currency"] == "EUR"
    assert cards.assign(store.get_task("odd"), generated)["id"] == "other"
    # A second pass does not ask again for a cluster that already has its kind; invalid answers are ignored.
    assert asyncio.run(cards.design_card_kinds(rt, NOW)) == {"clusters": 1, "new": 0, "designed": 0} and len(llm.calls) == 1
    assert cards.validate_kinds({"kinds": [{"cluster": "c", "worth_it": True, "id": "x", "layout": "nope", "keywords": ["a", "b"]}]}, {}) == []
    store.set_card_kind_status("invoice", "disabled", NOW_S)
    assert store.list_card_kinds(active_only=True) == [] or all(k["id"] != "invoice" for k in store.list_card_kinds(active_only=True))

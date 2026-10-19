from __future__ import annotations

import asyncio

from conftest import NOW, FakeContext, FakeLlm, active_config

from radar.delivery.learn import fallback_preference, forget_preference, learn_preference, list_preferences_text, notice_context, preference_from_fields, store_preference
from radar.delivery.telegram_ui import command_text
from radar.pipeline.attention import resolve
from radar.pipeline.triage import triage_signal
from radar.runtime import Runtime

NOW_S = "2026-10-19T10:00:00Z"


def _seed(store):
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=NOW_S)
    store.create_task(task_id="t1", kind="check", title="Alertas de CI de DMS", summary="CI falla", entities={}, expectations=[], checklist=[], now=NOW_S)
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at=NOW_S, external_ref="m1", group_key="g", title="[CI] build failed",
                     snippet="x", meta={"sender": "GitHub <ci@github.com>", "account": "me@example.org", "source_kind": "gmail"}, fingerprint="f1", now=NOW_S)
    store.link_task_signal("t1", "s1", "origin", NOW_S)


def _signal(sender="GitHub <ci@github.com>", kind="email"):
    return {"id": "x", "kind": kind, "title": "[CI] build failed", "snippet": "", "meta": {"sender": sender, "account": "me@example.org", "source_kind": "gmail"}}


def test_model_interprets_the_reply_stores_a_scoped_preference_and_triage_applies_it(store):
    cfg = active_config()
    _seed(store)
    answer = {"applies": True, "dimension": "notification", "effect": "digest", "scope": {"sender": "ci@github.com", "account": "", "message_type": "", "project": "", "kind": "", "source_kind": "", "repo": ""},
              "exceptions": ["security"], "instruction": "Los avisos de CI de GitHub solo en el resumen.", "why": "the owner wants CI mail batched"}
    llm = FakeLlm({"radar.preference": [answer, {**answer, "applies": False}]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    assert notice_context(store, "t1")["sender"] == "ci@github.com" and notice_context(store, "nope")["sender"] == ""
    result = asyncio.run(learn_preference(rt, text="los de CI mándamelos en el resumen", task_id="t1", actor="telegram:7", now_dt=NOW))
    pref = result["preference"]
    assert result["outcome"] == "preference" and pref["effect"] == "digest" and pref["scope"] == {"sender": "ci@github.com"} and pref["exceptions"] == ["security"]
    assert "solo en el resumen" in result["text"] and "remitente ci@github.com" in result["text"] and f"/radar forget {pref['id']}" in result["text"]
    assert store.list_preferences(active_only=True)[0]["id"] == pref["id"] and store.list_audit(subject_id=pref["id"])[0]["decision_code"] == "preference_learned"
    # Attention and triage honour it; a protected flag still breaks through.
    active = store.list_preferences(active_only=True)
    observation = {"account": "me@example.org", "sender": "GitHub <ci@github.com>", "message_type": "email", "kind": "check", "source_kind": "gmail", "project": "", "repo": ""}
    assert resolve({**observation, "sender": "ci@github.com"}, active, model_decision="notify_now").disposition == "digest"
    assert resolve({**observation, "sender": "ci@github.com"}, active, model_decision="notify_now", risk_flags=["security"]).disposition == "notify_now"
    # A reply the model says is no lasting rule is answered as unclear, and nothing new is stored.
    unclear = asyncio.run(learn_preference(rt, text="gracias", task_id="t1", actor="telegram:7", now_dt=NOW))
    assert unclear["outcome"] == "unclear" and len(store.list_preferences()) == 1
    # Restating the same rule makes a new version instead of a duplicate; forgetting disables it.
    again = store_preference(store, {"dimension": "notification", "effect": "digest", "scope": {"sender": "ci@github.com"}, "exceptions": (), "instruction": "CI en resumen"}, origin="explicit", now_dt=NOW)
    assert again["id"] == pref["id"] and again["version"] == 2 and len(store.list_preferences()) == 1
    assert "CI en resumen" in command_text(rt, "prefs")
    assert command_text(rt, f"forget {pref['id']}").startswith("Preferencia desactivada") and store.list_preferences(active_only=True) == []
    assert command_text(rt, "forget pref-nope").startswith("No encuentro") and command_text(rt, "prefs").startswith("No hay preferencias")


def test_without_a_model_the_owners_words_become_a_sender_scoped_rule_and_sources_refresh(store):
    cfg = active_config()
    _seed(store)
    rt = Runtime(FakeContext(cfg, llm=None), store, cfg, worker_id="w", clock=lambda: NOW)

    class Adapter:
        preferences: list = []

    rt.sources = {"src": Adapter()}
    result = asyncio.run(learn_preference(rt, text="No me avises más de este remitente", task_id="t1", actor="telegram:7", now_dt=NOW))
    pref = result["preference"]
    assert pref["effect"] == "silent" and pref["scope"] == {"sender": "ci@github.com"} and pref["instruction"] == "No me avises más de este remitente"
    assert rt.sources["src"].preferences and rt.sources["src"].preferences[0]["id"] == pref["id"]
    decision = triage_signal({**_signal(), "meta": {**_signal()["meta"], "sender": "ci@github.com"}}, rt.sources["src"].preferences, cfg)
    assert decision.keep is False
    assert triage_signal({**_signal(), "meta": {**_signal()["meta"], "sender": "other@example.org"}}, rt.sources["src"].preferences, cfg).keep is True
    # Wording decides the effect; account cues widen the scope; no context leaves a general rule.
    context = notice_context(store, "t1")
    assert fallback_preference("avísame siempre de esto", context)["effect"] == "notify"
    assert fallback_preference("agrúpalo en el resumen", context) == {"dimension": "notification", "effect": "digest", "scope": {"kind": "check"}, "exceptions": (), "instruction": "agrúpalo en el resumen"}
    assert fallback_preference("silencia toda esta cuenta", context)["scope"] == {"account": "me@example.org"}
    assert fallback_preference("no me avises de esto", notice_context(store, None))["scope"] == {}
    # The agent-facing tool validates fields the same way.
    direct = preference_from_fields({"instruction": "Interrumpir siempre por DMS", "effect": "notify", "scope": {"repo": "MalditaEs/DMS", "bogus": "x"}, "exceptions": ["deadline", "nope"]})
    assert direct == {"dimension": "notification", "effect": "notify", "scope": {"repo": "MalditaEs/DMS"}, "exceptions": ("deadline",), "instruction": "Interrumpir siempre por DMS"}
    try:
        preference_from_fields({"instruction": "", "effect": "silent"})
        raise AssertionError("empty instruction accepted")
    except ValueError:
        pass
    assert forget_preference(store, "missing", now_dt=NOW) is None

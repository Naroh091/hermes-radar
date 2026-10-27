from __future__ import annotations

from conftest import FakeContext, active_config, config

from radar.config import load_config
from radar.schemas import validate_config


def test_gmail_source_validation_and_trust_rules():
    cfg = config(sources=[{"id": "gmail-a", "kind": "gmail", "address": "a@example.org", "server": "gmail-a", "push": {"enabled": True, "topic": "", "credentials_path": ""}}])
    ok, errors = validate_config(cfg)
    assert not ok and any("push requires topic" in e for e in errors)
    cfg["sources"][0]["push"] = {"enabled": False}
    cfg["sources"].append({"id": "cal", "kind": "calendar", "server": "google-calendar"})
    cfg["sources"].append({"id": "gh", "kind": "github", "repos": ["org/app"]})
    assert validate_config(cfg) == (True, [])
    cfg["sources"][0]["token"] = "nope"
    assert "sources[0] has unknown keys: token" in validate_config(cfg)[1]
    bad = config(trust={"level": "yolo", "overrides": {"trip": {"local": "sometimes"}}, "auto_external": []})
    errors = validate_config(bad)[1]
    assert any("trust.level" in e for e in errors) and any("trust.overrides" in e for e in errors)
    receiver = config(ingress={"gmail_receiver": {"enabled": True, "port": 70000, "require_oidc": True}})
    assert any("port" in e for e in validate_config(receiver)[1])


def test_load_config_merges_partial_sections_from_context():
    ctx = FakeContext({"enabled": True, "owner": {"id": "me"}, "trust": {"level": "cautious"}, "jobs": {"max_turns": 5}})
    cfg = load_config(ctx)
    assert cfg["trust"] == {"level": "cautious", "overrides": {}, "auto_external": []}
    assert cfg["jobs"]["max_turns"] == 5 and cfg["jobs"]["max_seconds"] == 300 and cfg["ingress"]["gmail_receiver"]["port"] == 8666
    assert validate_config(active_config()) == (True, [])


def test_flat_trust_level_wins_over_nested_level_and_is_validated():
    ctx = FakeContext({"enabled": True, "owner": {"id": "me"}, "trust": {"level": "balanced"}, "trust_level": "cautious"})
    assert load_config(ctx)["trust"]["level"] == "cautious"
    ok, errors = validate_config(config(trust_level="yolo"))
    assert not ok and any("trust_level" in e for e in errors)
    assert validate_config(config(trust_level=None)) == (True, [])


def test_flat_job_limits_from_the_desktop_form_override_nested_sections():
    ctx = FakeContext({"enabled": True, "owner": {"id": "me"}, "jobs_per_day": 200, "auto_jobs_per_task": 5, "auto_job_cooldown_minutes": 0,
                       "notices_per_day": 20, "quiet_start": 1320})
    cfg = load_config(ctx)
    assert cfg["limits"]["jobs_per_day"] == 200 and cfg["jobs"]["max_auto_per_task"] == 5 and cfg["jobs"]["auto_cooldown_minutes"] == 0
    assert cfg["notifications"]["daily_limit"] == 20 and cfg["notifications"]["quiet_start"] == "22:00"
    assert config()["limits"]["jobs_per_day"] == 100
    assert any("jobs_per_day must be a positive integer" in e for e in validate_config(config(jobs_per_day=0))[1])


def test_reminder_pace_is_configurable_from_the_desktop_form():
    defaults = load_config(FakeContext({"enabled": True, "owner": {"id": "me"}}))["notifications"]
    assert defaults["reminders_per_day"] == 20 and defaults["reminder_gap_minutes"] == 0 and defaults["digest_times"] == []
    cfg = load_config(FakeContext({"enabled": True, "owner": {"id": "me"}, "reminders_per_day": 8, "reminder_gap_minutes": 45,
                                   "notifications": {"reminders_per_day": 3}}))
    assert cfg["notifications"]["reminders_per_day"] == 8 and cfg["notifications"]["reminder_gap_minutes"] == 45
    assert any("reminders_per_day must be a positive integer" in e for e in validate_config(config(reminders_per_day=0))[1])
    assert any("reminder_gap_minutes must be a non-negative" in e for e in validate_config(config(reminder_gap_minutes=-5))[1])

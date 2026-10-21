"""The tool firewall: what a delegated step may not do, whatever the prompt or a failing tool suggests."""
from __future__ import annotations

import asyncio
import json

from radar.jobs import firewall
from radar.jobs.firewall import allowed_tools_for, child_env, guard_tool_call, is_external, tool_stem
from radar.jobs.runner import JobRunner


def test_external_tools_are_known_by_their_stem_whatever_server_serves_them():
    assert tool_stem("mcp__gmail_maldita__reply_all") == "reply_all" and tool_stem("cronjob_manage") == "cronjob_manage"
    for name in ("mcp__gmail_work__reply_all", "mcp__gmail_work__send_email", "mcp__gmail_work__send_draft", "mcp__agentmail__reply_to_message",
                 "mcp__github__merge_pull_request", "mcp__github__add_issue_comment", "mcp__github__create_issue", "mcp__google_calendar__respond_to_event",
                 "mcp__google_calendar__create_event", "mcp__gmail_work__delete_email", "mcp__gmail_work__batch_delete_emails", "cronjob_manage",
                 "send_message", "yb_send_dm", "mcp__slack__post_message", "mcp__drive__share_file", "mcp__gmail_work__forward_email"):
        assert is_external(tool_stem(name)), name
    for name in ("mcp__gmail_work__search_emails", "mcp__gmail_work__read_email", "mcp__gmail_work__get_thread", "mcp__gmail_work__draft_email",
                 "mcp__gmail_work__create_draft", "mcp__gmail_work__modify_email", "mcp__github__get_pull_request", "mcp__github__list_commits",
                 "mcp__github__create_branch", "mcp__github__push_files", "mcp__google_calendar__list_events", "mcp__linear__get_issue",
                 "web_search", "web_extract", "read_file", "write_file", "patch", "terminal", "tool_search", "tool_describe", "browser_navigate",
                 "mcp__agentmail__list_replies", "mcp__github__list_deployments", "skill_view"):
        assert not is_external(tool_stem(name)), name


def test_a_read_or_local_step_cannot_send_even_when_its_draft_tool_failed():
    env = {"HERMES_RADAR_JOB": "1", "HERMES_RADAR_JOB_CLASS": "local"}
    verdict = guard_tool_call(tool_name="mcp__gmail_maldita__reply_all", args={"messageId": "1a15f3af2614e435", "body": "Hola"}, environ=env)
    assert verdict["action"] == "block" and "local step" in verdict["message"] and "proposed_reply" in verdict["message"]
    assert guard_tool_call(tool_name="mcp__gmail_maldita__send_email", environ=env)["action"] == "block"
    assert guard_tool_call(tool_name="cronjob_manage", args={"action": "create"}, environ=env)["action"] == "block"
    assert guard_tool_call(tool_name="mcp__gmail_maldita__draft_email", environ=env) is None  # a draft is the way to go
    assert guard_tool_call(tool_name="mcp__gmail_maldita__search_emails", environ=env) is None
    assert guard_tool_call(tool_name="terminal", args={"command": "git status"}, environ=env) is None


def test_a_consented_external_step_gets_exactly_its_tools():
    env = {"HERMES_RADAR_JOB": "1", "HERMES_RADAR_JOB_CLASS": "external", "HERMES_RADAR_JOB_ALLOW": allowed_tools_for("gmail.reply")}
    assert guard_tool_call(tool_name="mcp__gmail_maldita__reply_all", environ=env) is None
    assert guard_tool_call(tool_name="mcp__gmail_maldita__delete_email", environ=env)["action"] == "block"
    assert guard_tool_call(tool_name="cronjob_manage", environ=env)["action"] == "block"
    assert allowed_tools_for("calendar.respond") == "respond_to_event" and allowed_tools_for("github.merge_pr").startswith("merge_pull_request")
    assert "send_email" in allowed_tools_for("gmail.compose_new") and allowed_tools_for("web.act") == "" and allowed_tools_for("") == ""
    # an external step without a nameable tool (a browser check-in) still cannot mail anyone
    bare = {"HERMES_RADAR_JOB": "1", "HERMES_RADAR_JOB_CLASS": "external"}
    assert guard_tool_call(tool_name="mcp__gmail_maldita__send_email", environ=bare)["action"] == "block"


def test_the_hook_is_inert_outside_a_job_child(monkeypatch):
    monkeypatch.delenv("HERMES_RADAR_JOB", raising=False)
    assert guard_tool_call(tool_name="mcp__gmail_maldita__reply_all", args={}) is None  # the gateway's own sessions keep their tools
    assert guard_tool_call(tool_name="mcp__gmail_maldita__reply_all", environ={}) is None


def test_the_runner_hands_the_child_its_class_and_allow_list():
    runner = JobRunner(spawn=None, max_seconds=10, env={"PATH": "/bin", "HERMES_RADAR_JOB_ALLOW": "stale"})
    local = runner._child_env({"id": "j", "action_class": "local"})
    assert local["HERMES_RADAR_JOB"] == "1" and local["HERMES_RADAR_JOB_CLASS"] == "local" and "HERMES_RADAR_JOB_ALLOW" not in local
    assert local["HERMES_ACCEPT_HOOKS"] == "1"  # the firewall hook must run in the child
    external = runner._child_env({"id": "j", "action_class": "external"}, allow="reply_all, send_email")
    assert external["HERMES_RADAR_JOB_ALLOW"] == "reply_all,send_email"
    assert child_env("read")["HERMES_RADAR_JOB_CLASS"] == "read" and "HERMES_RADAR_JOB_ALLOW" not in child_env("local", "send_email")

    seen = {}

    async def spawn(argv, stdin, timeout):
        seen["argv"] = argv
        return 0, json.dumps({"type": "result", "text": '{"summary": "hecho"}', "session_id": "s"}), ""

    outcome = asyncio.run(JobRunner(spawn=spawn, max_seconds=10).run({"id": "j", "prompt": "p", "action_class": "local"}, allow="reply_all"))
    assert outcome.status == "succeeded" and seen["argv"][1] == "chat"


def test_register_wires_the_firewall_hook():
    import radar

    class Ctx:
        def __init__(self):
            self.hooks, self.tools = {}, {}
            self.profile_name = "default"

        def register_tool(self, name, **kw):
            self.tools[name] = kw

        def register_hook(self, name, fn):
            self.hooks[name] = fn

        def register_platform_handler(self, *a, **k):
            pass

        def register_cli_command(self, *a, **k):
            pass

        def register_command(self, *a, **k):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: None

    ctx = Ctx()
    radar.register(ctx)
    assert "pre_tool_call" in ctx.hooks
    assert ctx.hooks["pre_tool_call"](tool_name="mcp__gmail_maldita__reply_all", args={}, session_id="s") is None  # not in a job child here
    assert firewall.JOB_ENV == "HERMES_RADAR_JOB"

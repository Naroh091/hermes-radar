from __future__ import annotations

import json

from conftest import NOW, FakeContext, config

from radar.ingress.webhook_consumer import WebhookConsumer
from radar.pipeline.extract import signal_view
from radar.pipeline.triage import triage_signal
from radar.providers.base import ProviderError
from radar.providers.repo_gh import GhRepoProvider, split_http_response
from radar.runtime import Runtime
from radar.sources.github import GithubSource, classify_payload, normalise_api_event, normalise_webhook, wanted

REPO = {"full_name": "MalditaEs/DMS", "default_branch": "master"}


def pr_payload(action="opened", merged=False, draft=False, user="ftello"):
    return {"action": action, "repository": REPO, "sender": {"login": user},
            "pull_request": {"number": 2009, "title": "claims: corregir el autocompletado", "body": "Arregla #2001", "html_url": "https://github.com/MalditaEs/DMS/pull/2009",
                             "state": "closed" if action == "closed" else "open", "merged": merged, "draft": draft, "labels": [{"name": "bug"}], "head": {"ref": "ftello/dms-365"},
                             "user": {"login": user}, "updated_at": "2026-10-19T18:18:44Z", "id": 991}}


def push_payload(ref="refs/heads/master", commits=2):
    return {"ref": ref, "before": "a" * 40, "after": "b" * 40, "compare": "https://github.com/MalditaEs/DMS/compare/a...b", "repository": REPO,
            "pusher": {"name": "cponce"}, "sender": {"login": "cponce"}, "head_commit": {"timestamp": "2026-10-19T18:00:00Z"},
            "commits": [{"message": f"commit {i}\n\ndetails"} for i in range(commits)]}


def workflow_payload(conclusion="failure", status="completed"):
    return {"action": "completed", "repository": REPO, "sender": {"login": "github-actions[bot]"},
            "workflow_run": {"id": 55, "name": "CI", "html_url": "https://github.com/MalditaEs/DMS/actions/runs/55", "head_branch": "master", "head_sha": "b" * 40,
                             "status": status, "conclusion": conclusion, "updated_at": "2026-10-19T18:05:00Z", "actor": {"login": "cponce"}}}


def deployment_status_payload(state="success"):
    return {"action": "created", "repository": REPO, "sender": {"login": "vercel[bot]"},
            "deployment": {"id": 7, "environment": "staging", "ref": "master", "sha": "b" * 40, "description": "", "updated_at": "2026-10-19T18:10:00Z"},
            "deployment_status": {"id": 70, "state": state, "target_url": "https://staging.example", "updated_at": "2026-10-19T18:11:00Z"}}


def test_webhook_payloads_are_classified_filtered_and_turned_into_signals(store):
    assert classify_payload(pr_payload()) == ("pull_request", "opened") and classify_payload(push_payload()) == ("push", "")
    assert classify_payload(workflow_payload()) == ("workflow_run", "completed") and classify_payload({"zen": "x"}) == ("unknown", "")
    settings = {"repos": ["MalditaEs/DMS"], "events": list(("pull_request", "push", "workflow_run", "deployment_status", "issues"))}
    assert wanted(normalise_webhook(pr_payload()), settings) == (True, "github")
    assert wanted(normalise_webhook(pr_payload("synchronize")), settings)[0] is False
    assert wanted(normalise_webhook(push_payload("refs/heads/feature/x")), settings) == (False, "push_other_branch")
    assert wanted(normalise_webhook(push_payload("refs/heads/feature/x")), {**settings, "all_branches": True})[0] is True
    assert wanted(normalise_webhook(workflow_payload("success")), settings) == (False, "workflow_not_failed")
    assert wanted(normalise_webhook(deployment_status_payload("pending")), settings) == (False, "deployment_in_progress")
    other = normalise_webhook({**pr_payload(), "repository": {"full_name": "Other/repo", "default_branch": "main"}})
    assert wanted(other, settings) == (False, "repo_not_watched")
    store.sync_source(source_id="gh", kind="github", account_ref="MalditaEs/DMS", config={"repos": ["MalditaEs/DMS"], "route": "github-pr"},
                      resource_scope={"repos": ["MalditaEs/DMS"]}, retention_days=30, enabled=True, operations=["repo.read"], now="2026-10-19T08:00:00Z")
    source = GithubSource(store.get_source("configured:gh"), config(), store, provider=None, clock=lambda: NOW)
    assert source.due(NOW) is False  # webhook-only: nothing to poll
    assert source.handle_event(pr_payload(), "d1", NOW) == "added"
    assert source.handle_event(pr_payload(), "d1", NOW) == "duplicate"
    assert source.handle_event(pr_payload("synchronize"), "d2", NOW) == "skipped"
    assert source.handle_event(push_payload(), "d3", NOW) == "added"
    assert source.handle_event(workflow_payload(), "d4", NOW) == "added"
    assert source.handle_event(deployment_status_payload(), "d5", NOW) == "added"
    assert source.handle_event(pr_payload("closed", merged=True), "d6", NOW) == "added"
    signals = {s["external_ref"]: s for s in store.list_signals(kind="github", limit=20)}
    pr = signals["pull_request:d1"]
    assert pr["title"] == "PR #2009 opened: claims: corregir el autocompletado" and pr["group_key"] == "github:MalditaEs/DMS:pr:2009" and pr["meta"]["labels"] == ["bug"]
    assert signals["pull_request:d6"]["title"].startswith("PR #2009 merged") and signals["push:d3"]["title"].startswith("Push to master by cponce (2 commits)")
    assert signals["workflow_run:d4"]["title"] == "Workflow CI failure on master" and signals["workflow_run:d4"]["triage_status"] == "pending"
    assert signals["deployment_status:d5"]["title"] == "Deployment to staging success (master)" and signals["deployment_status:d5"]["group_key"] == "github:MalditaEs/DMS:deploy:staging"
    view = signal_view(pr, budget=3000)
    assert view["repo"] == "MalditaEs/DMS" and view["event_type"] == "pull_request" and view["number"] == 2009 and view["url"].endswith("/pull/2009")
    # Bots are dropped by triage unless they report a run or a deployment outcome.
    bot = {"kind": "github", "title": "x", "snippet": "", "meta": {"actor": "dependabot[bot]", "event_type": "pull_request"}}
    assert triage_signal(bot, [], config()).keep is False and triage_signal({**bot, "meta": {"actor": "github-actions[bot]", "event_type": "workflow_run"}}, [], config()).keep is True


def test_gh_provider_polls_with_etag_and_posts_comments_only_when_allowed():
    calls = []

    def runner(argv, stdin):
        calls.append((argv, stdin))
        if argv[:2] == ["api", "-i"] and "If-None-Match" in " ".join(argv):
            return 1, 'HTTP/2.0 304 Not Modified\nEtag: W/"abc"\n\n', "gh: HTTP 304"
        if argv[:2] == ["api", "-i"]:
            body = json.dumps([{"id": "101", "type": "PullRequestEvent", "created_at": "2026-10-19T18:18:44Z", "actor": {"login": "ftello"},
                                "repo": {"name": "MalditaEs/DMS"}, "payload": {"action": "opened", "number": 2009, "pull_request": pr_payload()["pull_request"]}},
                               {"id": "100", "type": "PushEvent", "created_at": "2026-10-17T22:40:27Z", "actor": {"login": "cponce"}, "repo": {"name": "MalditaEs/DMS"},
                                "payload": {"ref": "refs/heads/master", "head": "b" * 40, "before": "a" * 40, "commits": [{"message": "fix"}]}},
                               {"id": "99", "type": "CreateEvent", "created_at": "2026-10-17T22:00:00Z", "actor": {"login": "x"}, "repo": {"name": "MalditaEs/DMS"}, "payload": {"ref_type": "branch"}}])
            return 0, 'HTTP/2.0 200 OK\nEtag: W/"abc"\nX-Poll-Interval: 60\n\n' + body, ""
        if argv[:2] == ["api", "-X"]:
            return 0, json.dumps({"id": 5001, "html_url": "https://github.com/MalditaEs/DMS/pull/2009#issuecomment-5001", "created_at": "t"}), ""
        if argv[0] == "api" and "/issues/comments/5001" in argv[1]:
            return 0, json.dumps({"id": 5001, "body": "hola"}), ""
        return 1, "", "unexpected"

    status, headers, body = split_http_response('HTTP/1.1 200 OK\r\nEtag: "x"\r\n\r\n[1]')
    assert (status, headers["etag"], body) == (200, '"x"', "[1]")
    provider = GhRepoProvider(runner=runner)
    items, etag = provider.events("MalditaEs/DMS")
    assert [i["id"] for i in items] == ["101", "100", "99"] and etag == 'W/"abc"'
    again, etag2 = provider.events("MalditaEs/DMS", etag=etag)
    assert again == [] and etag2 == etag and calls[1][0][2:4] == ["-H", 'If-None-Match: W/"abc"']
    events = [normalise_api_event(i) for i in items]
    assert events[0]["event_type"] == "pull_request" and events[0]["number"] == 2009 and events[0]["id"] == "101" and events[0]["actor"] == "ftello"
    assert events[1]["event_type"] == "push" and events[1]["ref"] == "refs/heads/master" and events[1]["commits"] == ["fix"] and events[2] is None
    try:
        provider.comment("MalditaEs/DMS", 2009, "hola")
        raise AssertionError("writes must be refused by default")
    except PermissionError:
        pass
    writer = GhRepoProvider(runner=runner, allow_write=True)
    created = writer.comment("MalditaEs/DMS", 2009, "hola")
    assert created["id"] == 5001 and created["url"].endswith("issuecomment-5001") and json.loads(calls[-1][1]) == {"body": "hola"}
    assert writer.get_comment("MalditaEs/DMS", 5001)["id"] == 5001
    try:
        provider.events("not a repo")
        raise AssertionError("bad repo accepted")
    except ValueError:
        pass
    try:
        GhRepoProvider(runner=lambda a, s: (1, "HTTP/2.0 404 Not Found\n\n{}", "gh: Not Found")).events("x/y")
        raise AssertionError("404 accepted")
    except ProviderError:
        pass


def test_polling_source_baselines_then_ingests_new_events_and_runtime_routes_webhooks(store):
    class Repo:
        def __init__(self):
            self.items = [{"id": "100", "type": "PushEvent", "created_at": "2026-10-17T22:40:27Z", "actor": {"login": "cponce"}, "repo": {"name": "MalditaEs/DMS"},
                           "payload": {"ref": "refs/heads/master", "head": "b" * 40, "before": "a" * 40, "commits": [{"message": "fix"}]}}]
            self.etags: list = []

        def events(self, repo, *, etag=None, per_page=50):
            self.etags.append(etag)
            return list(self.items), 'W/"e1"'

    repo = Repo()
    store.sync_source(source_id="gh", kind="github", account_ref="MalditaEs/DMS", config={"repos": ["MalditaEs/DMS"], "interval_seconds": 600},
                      resource_scope={"repos": ["MalditaEs/DMS"]}, retention_days=30, enabled=True, operations=["repo.read"], now="2026-10-19T08:00:00Z")
    source = GithubSource(store.get_source("configured:gh"), config(), store, provider=repo, clock=lambda: NOW)
    assert source.due(NOW) is True
    first = source.sync(NOW)
    assert first.added == 0 and first.cursor["last_event_ids"] == {"MalditaEs/DMS": "100"} and first.cursor["etags"] == {"MalditaEs/DMS": 'W/"e1"'}
    store.update_source_cursor(source.id, first.cursor, "2026-10-19T10:00:00Z", success=True)
    source.row = store.get_source(source.id)
    repo.items.insert(0, {"id": "101", "type": "PullRequestEvent", "created_at": "2026-10-19T18:18:44Z", "actor": {"login": "ftello"}, "repo": {"name": "MalditaEs/DMS"},
                          "payload": {"action": "opened", "number": 2009, "pull_request": pr_payload()["pull_request"]}})
    second = source.sync(NOW)
    assert second.added == 1 and second.cursor["last_event_ids"]["MalditaEs/DMS"] == "101" and repo.etags[-1] == 'W/"e1"'
    signal = store.list_signals(kind="github", limit=5)[0]
    assert signal["external_ref"] == "pull_request:101" and signal["meta"]["number"] == 2009
    # The runtime registers the source's route (observe only unless consume) and hands deliveries to it.
    cfg = config(sources=[{"id": "gh", "kind": "github", "repos": ["MalditaEs/DMS"], "route": "github-pr", "consume": False}])
    rt = Runtime(FakeContext(cfg, llm=None), store, cfg, worker_id="w", clock=lambda: NOW)
    assert rt.routes()["github-pr"] == {"kind": "github", "skip": False}
    rt.sources = {"configured:gh": GithubSource(store.get_source("configured:gh"), cfg, store, provider=None, clock=lambda: NOW)}
    rt.sources["configured:gh"].row["config"]["route"] = "github-pr"
    consumer = WebhookConsumer(rt.routes(), on_gmail_push=lambda p: None, on_github=rt.on_github)

    class Event:
        def __init__(self, user_id, raw, message_id):
            self.source = type("S", (), {"user_id": user_id})()
            self.raw_message = raw
            self.message_id = message_id

    assert consumer.on_event(Event("webhook:github-pr", pr_payload("reopened"), "d9")) is None  # observed, the gatekeeper still runs
    assert "pull_request:d9" in {s["external_ref"] for s in store.list_signals(kind="github", limit=10)}
    assert consumer.on_event(Event("webhook:other", {"x": 1}, "d10")) is None


def test_gh_provider_opens_draft_prs_and_finds_them_by_branch():
    calls = []

    def runner(argv, stdin):
        calls.append((argv, stdin))
        if argv[:2] == ["pr", "list"]:
            return 0, json.dumps([{"number": 2010, "url": "https://github.com/MalditaEs/DMS/pull/2010", "isDraft": True, "state": "OPEN", "title": "t"}] if "radar/x" in argv else []), ""
        if argv[:2] == ["pr", "create"]:
            return 0, "Creating draft pull request...\nhttps://github.com/MalditaEs/DMS/pull/2010\n", ""
        if argv[0] == "api" and argv[1] == "repos/MalditaEs/DMS":
            return 0, json.dumps({"default_branch": "master"}), ""
        return 1, "", "unexpected"

    provider = GhRepoProvider(runner=runner, allow_write=True)
    assert provider.default_branch("MalditaEs/DMS") == "master"
    assert provider.pull_request_by_branch("MalditaEs/DMS", "MalditaEs:radar/x")["number"] == 2010 and provider.pull_request_by_branch("MalditaEs/DMS", "other") is None
    created = provider.open_pr("MalditaEs/DMS", head="radar/x", base="master", title="Fix", body="body\n\nmark")
    assert created == {"number": 2010, "url": "https://github.com/MalditaEs/DMS/pull/2010", "draft": True}
    argv, body = calls[-1]
    assert "--draft" in argv and argv[argv.index("--head") + 1] == "radar/x" and body == "body\n\nmark"
    try:
        provider.open_pr("MalditaEs/DMS", head="radar/x", base="master", title="Fix", body="b", draft=False)
        raise AssertionError("non-draft PR accepted")
    except ValueError:
        pass
    try:
        GhRepoProvider(runner=runner).open_pr("MalditaEs/DMS", head="radar/x", base="master", title="Fix", body="b")
        raise AssertionError("write without allow_write accepted")
    except PermissionError:
        pass

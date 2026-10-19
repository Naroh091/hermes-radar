"""GitHub as a source: the host's webhook route (observed in ``pre_gateway_dispatch``) and, as a fallback
or on its own, the public events API polled through ``gh`` with an ETag.

Both paths normalise into the same event dict and the same filters, so a repo without a webhook still
produces signals (a few minutes later). The first poll only records the newest event id (baseline).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from ..pipeline.triage import triage_signal
from ..providers.base import ProviderError
from .base import SourceAdapter, SyncResult, fingerprint, iso, parse_iso

log = logging.getLogger(__name__)

DEFAULT_EVENTS = ("pull_request", "push", "issues", "issue_comment", "pull_request_review", "workflow_run", "deployment_status", "release")
PR_ACTIONS = {"opened", "reopened", "ready_for_review", "closed", "review_requested", "edited_title"}
ISSUE_ACTIONS = {"opened", "reopened", "closed", "assigned"}
FAILED_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required"}
MAX_COMMITS = 5


def _login(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("login") or value.get("name") or "")
    return str(value or "")


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def classify_payload(payload: dict[str, Any]) -> tuple[str, str]:
    """GitHub webhook payload → (event_type, action) without the ``X-GitHub-Event`` header."""
    action = str(payload.get("action") or "")
    if "workflow_run" in payload:
        return "workflow_run", action
    if "deployment_status" in payload:
        return "deployment_status", action
    if "deployment" in payload:
        return "deployment", action
    if "comment" in payload and "issue" in payload:
        return "issue_comment", action
    if "comment" in payload and "pull_request" in payload:
        return "pull_request_review_comment", action
    if "review" in payload and "pull_request" in payload:
        return "pull_request_review", action
    if "pull_request" in payload:
        return "pull_request", action
    if "issue" in payload:
        return "issues", action
    if "release" in payload:
        return "release", action
    if "commits" in payload or ("ref" in payload and "before" in payload and "after" in payload):
        return "push", ""
    return "unknown", action


def normalise_webhook(payload: dict[str, Any], delivery_id: str = "") -> dict[str, Any] | None:
    event_type, action = classify_payload(payload)
    if event_type == "unknown":
        return None
    repo = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    event: dict[str, Any] = {
        "event_type": event_type, "action": action, "repo": str(repo.get("full_name") or ""), "default_branch": str(repo.get("default_branch") or ""),
        "actor": _login(payload.get("sender")), "delivery_id": str(delivery_id or ""), "number": None, "title": "", "body": "", "url": "",
        "ref": "", "sha": "", "state": "", "merged": False, "draft": False, "labels": [], "commits": [], "conclusion": "", "environment": "",
        "created_at": "", "id": "",
    }
    if event_type in ("pull_request", "pull_request_review", "pull_request_review_comment"):
        pr = payload.get("pull_request") or {}
        event.update({"number": pr.get("number"), "title": str(pr.get("title") or ""), "body": str(pr.get("body") or ""), "url": str(pr.get("html_url") or ""),
                      "state": str(pr.get("state") or ""), "merged": bool(pr.get("merged")), "draft": bool(pr.get("draft")),
                      "labels": [str(l.get("name")) for l in pr.get("labels") or [] if isinstance(l, dict)], "ref": str((pr.get("head") or {}).get("ref") or ""),
                      "author": _login(pr.get("user")), "created_at": str(pr.get("updated_at") or pr.get("created_at") or ""), "id": str(pr.get("id") or "")})
        if event_type == "pull_request_review":
            review = payload.get("review") or {}
            event.update({"state": str(review.get("state") or ""), "body": str(review.get("body") or ""), "url": str(review.get("html_url") or event["url"]), "id": str(review.get("id") or "")})
        elif event_type == "pull_request_review_comment":
            comment = payload.get("comment") or {}
            event.update({"body": str(comment.get("body") or ""), "url": str(comment.get("html_url") or event["url"]), "id": str(comment.get("id") or "")})
    elif event_type in ("issues", "issue_comment"):
        issue = payload.get("issue") or {}
        event.update({"number": issue.get("number"), "title": str(issue.get("title") or ""), "body": str(issue.get("body") or ""), "url": str(issue.get("html_url") or ""),
                      "state": str(issue.get("state") or ""), "labels": [str(l.get("name")) for l in issue.get("labels") or [] if isinstance(l, dict)],
                      "author": _login(issue.get("user")), "created_at": str(issue.get("updated_at") or ""), "id": str(issue.get("id") or "")})
        if event_type == "issue_comment":
            comment = payload.get("comment") or {}
            event.update({"body": str(comment.get("body") or ""), "url": str(comment.get("html_url") or event["url"]), "id": str(comment.get("id") or ""),
                          "created_at": str(comment.get("created_at") or "")})
            if "pull_request" in issue:
                event["event_type"] = "issue_comment"  # a comment on a PR arrives as an issue comment
    elif event_type == "push":
        commits = [c for c in payload.get("commits") or [] if isinstance(c, dict)]
        event.update({"ref": str(payload.get("ref") or ""), "sha": str(payload.get("after") or ""), "url": str(payload.get("compare") or ""),
                      "actor": _login(payload.get("pusher")) or event["actor"], "commits": [_clip(c.get("message"), 120) for c in commits[:MAX_COMMITS]],
                      "title": _clip((commits[-1].get("message") if commits else ""), 120), "created_at": str((payload.get("head_commit") or {}).get("timestamp") or ""),
                      "id": str(payload.get("after") or "")[:12], "commit_count": len(commits)})
    elif event_type == "workflow_run":
        run = payload.get("workflow_run") or {}
        event.update({"title": str(run.get("name") or ""), "url": str(run.get("html_url") or ""), "ref": str(run.get("head_branch") or ""), "sha": str(run.get("head_sha") or ""),
                      "state": str(run.get("status") or ""), "conclusion": str(run.get("conclusion") or ""), "created_at": str(run.get("updated_at") or ""), "id": str(run.get("id") or ""),
                      "actor": _login(run.get("actor")) or event["actor"]})
    elif event_type in ("deployment", "deployment_status"):
        deployment = payload.get("deployment") or {}
        status = payload.get("deployment_status") or {}
        event.update({"environment": str(deployment.get("environment") or ""), "ref": str(deployment.get("ref") or ""), "sha": str(deployment.get("sha") or "")[:12],
                      "state": str(status.get("state") or deployment.get("task") or ""), "url": str(status.get("target_url") or status.get("log_url") or deployment.get("url") or ""),
                      "title": str(deployment.get("description") or ""), "created_at": str(status.get("updated_at") or deployment.get("updated_at") or ""),
                      "id": str(status.get("id") or deployment.get("id") or "")})
    elif event_type == "release":
        release = payload.get("release") or {}
        event.update({"title": str(release.get("name") or release.get("tag_name") or ""), "body": str(release.get("body") or ""), "url": str(release.get("html_url") or ""),
                      "ref": str(release.get("tag_name") or ""), "draft": bool(release.get("draft")), "created_at": str(release.get("published_at") or ""), "id": str(release.get("id") or "")})
    return event


_API_TYPES = {"PullRequestEvent": "pull_request", "PushEvent": "push", "IssuesEvent": "issues", "IssueCommentEvent": "issue_comment",
              "PullRequestReviewEvent": "pull_request_review", "ReleaseEvent": "release", "CreateEvent": "create", "DeleteEvent": "delete"}


def normalise_api_event(item: dict[str, Any]) -> dict[str, Any] | None:
    """An item of ``GET /repos/{repo}/events`` into the same shape as a webhook event."""
    event_type = _API_TYPES.get(str(item.get("type") or ""))
    if event_type is None or event_type in ("create", "delete"):
        return None
    payload = dict(item.get("payload") or {})
    payload["repository"] = {"full_name": str((item.get("repo") or {}).get("name") or "")}
    payload["sender"] = item.get("actor") or {}
    if event_type == "push":
        payload.setdefault("commits", payload.get("commits") or [])
        payload["after"] = payload.get("head") or ""
        payload["before"] = payload.get("before") or ""
        payload["pusher"] = item.get("actor") or {}
    event = normalise_webhook(payload)
    if event is None:
        return None
    event["id"] = str(item.get("id") or event.get("id") or "")
    event["created_at"] = str(item.get("created_at") or event.get("created_at") or "")
    event["actor"] = _login(item.get("actor")) or event["actor"]
    return event


def wanted(event: dict[str, Any], settings: dict[str, Any]) -> tuple[bool, str]:
    """Keep what changes work: new or finished PRs and issues, pushes to the default branch, failed runs,
    deployment outcomes, published releases, reviews that ask for something, comments."""
    allowed = set(settings.get("events") or DEFAULT_EVENTS)
    kind, action = event["event_type"], event["action"]
    if kind not in allowed:
        return False, "event_not_watched"
    repos = [str(r).lower() for r in settings.get("repos") or []]
    if repos and event["repo"].lower() not in repos:
        return False, "repo_not_watched"
    if kind == "pull_request" and action and action not in PR_ACTIONS:
        return False, f"pr_{action}"
    if kind == "issues" and action and action not in ISSUE_ACTIONS:
        return False, f"issue_{action}"
    if kind == "push":
        default = event.get("default_branch") or "main"
        if not settings.get("all_branches") and event["ref"] not in (f"refs/heads/{default}", f"refs/heads/master", f"refs/heads/{default}".replace("refs/heads/refs/heads/", "refs/heads/")):
            return False, "push_other_branch"
        if not event.get("commits") and not event.get("sha"):
            return False, "push_empty"
    if kind == "workflow_run" and (event["state"] != "completed" or event["conclusion"] not in FAILED_CONCLUSIONS):
        return False, "workflow_not_failed"
    if kind == "deployment_status" and event["state"] not in {"success", "failure", "error", "inactive"}:
        return False, "deployment_in_progress"
    if kind == "release" and (action not in ("published", "") or event.get("draft")):
        return False, "release_not_published"
    if kind == "pull_request_review" and event["state"].lower() not in {"changes_requested", "approved"}:
        return False, "review_comment_only"
    return True, "github"


class GithubSource(SourceAdapter):
    kind = "github"

    def __init__(self, source_row: dict[str, Any], config: dict[str, Any], store: Any, *, provider: Any = None,
                 preferences: list[dict[str, Any]] | None = None, **kwargs: Any):
        super().__init__(source_row, config, store, **kwargs)
        self.provider = provider
        self.preferences = preferences or []
        self.webhook_events = 0

    @property
    def route(self) -> str:
        return str(self.settings.get("route") or "")

    @property
    def repos(self) -> list[str]:
        return [str(r) for r in self.settings.get("repos") or []]

    @property
    def interval_seconds(self) -> int:
        return max(60, int(self.settings.get("interval_seconds") or 600))

    def due(self, now_dt: datetime) -> bool:
        if self.provider is None or not self.repos:
            return False  # webhook-only source: nothing to poll
        return super().due(now_dt)

    # -- signals -----------------------------------------------------------------------------------
    def _signal(self, event: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
        snippet_chars = int(self.config.get("retention", {}).get("snippet_chars", 500))
        kind, action, number = event["event_type"], event["action"], event.get("number")
        repo = event["repo"]
        if kind == "pull_request":
            what = "merged" if action == "closed" and event.get("merged") else action or "updated"
            title = f"PR #{number} {what}: {event['title']}"
            group = f"github:{repo}:pr:{number}"
        elif kind in ("pull_request_review", "pull_request_review_comment", "issue_comment") and number:
            label = {"pull_request_review": f"review {event['state'].lower().replace('_', ' ')}", "pull_request_review_comment": "review comment", "issue_comment": "comment"}[kind]
            title = f"#{number} {label} by {event['actor']}: {event['title']}"
            group = f"github:{repo}:pr:{number}" if kind != "issue_comment" or event.get("url", "").find("/pull/") >= 0 else f"github:{repo}:issue:{number}"
        elif kind == "issues":
            title = f"Issue #{number} {action or 'updated'}: {event['title']}"
            group = f"github:{repo}:issue:{number}"
        elif kind == "push":
            branch = event["ref"].replace("refs/heads/", "")
            title = f"Push to {branch} by {event['actor']} ({event.get('commit_count') or len(event.get('commits') or [])} commits): {event['title']}"
            group = f"github:{repo}:push:{branch}"
        elif kind == "workflow_run":
            title = f"Workflow {event['title']} {event['conclusion']} on {event['ref']}"
            group = f"github:{repo}:workflow:{event['title']}:{event['ref']}"
        elif kind in ("deployment", "deployment_status"):
            title = f"Deployment to {event['environment']} {event['state']} ({event['ref'] or event['sha']})"
            group = f"github:{repo}:deploy:{event['environment']}"
        else:
            title = f"Release {event['title']} {action or 'published'}"
            group = f"github:{repo}:release"
        body = _clip(event.get("body") or " · ".join(event.get("commits") or []), snippet_chars)
        meta = {
            "source_kind": "github", "repo": repo, "event_type": kind, "action": action, "number": number, "actor": event.get("actor"),
            "author": event.get("author") or "", "url": event.get("url"), "ref": event.get("ref"), "sha": event.get("sha"), "state": event.get("state"),
            "merged": event.get("merged"), "draft": event.get("draft"), "labels": event.get("labels"), "commits": event.get("commits"),
            "conclusion": event.get("conclusion"), "environment": event.get("environment"), "delivery_id": event.get("delivery_id") or "",
        }
        occurred = parse_iso(event.get("created_at")) or now_dt
        marker = event.get("delivery_id") or event.get("id") or fingerprint(repo, kind, action, number, event.get("sha"), event.get("created_at"))[:16]
        return {
            "id": str(uuid4()), "kind": "github", "occurred_at": iso(occurred), "external_ref": f"{kind}:{marker}", "group_key": group,
            "title": title[:300], "snippet": body, "meta": meta, "fingerprint": fingerprint("github", repo, kind, action, number, event.get("sha"), event.get("id")),
        }

    def _ingest(self, signal: dict[str, Any], now_dt: datetime) -> str:
        decision = triage_signal(signal, self.preferences, self.config)
        added = self.store.add_signal(
            signal_id=signal["id"], source_id=self.id, kind="github", occurred_at=signal["occurred_at"], external_ref=signal["external_ref"],
            group_key=signal["group_key"], title=signal["title"], snippet=signal["snippet"], meta=signal["meta"], fingerprint=signal["fingerprint"],
            now=iso(now_dt), triage_status="pending" if decision.keep else "ignored", triage_reason=None if decision.keep else decision.reason,
            expires_at=self.signal_expiry(now_dt),
        )
        if not added:
            return "duplicate"
        return "added" if decision.keep else "ignored"

    def handle_event(self, payload: dict[str, Any], delivery_id: str, now_dt: datetime) -> str:
        """A webhook delivery from the host route. Returns added | ignored | duplicate | skipped."""
        self.webhook_events += 1
        event = normalise_webhook(payload, delivery_id)
        if event is None:
            return "skipped"
        keep, reason = wanted(event, self.settings)
        if not keep:
            return "skipped"
        return self._ingest(self._signal(event, now_dt), now_dt)

    # -- polling fallback --------------------------------------------------------------------------
    def sync(self, now_dt: datetime) -> SyncResult:
        result = SyncResult(cursor=dict(self.cursor))
        self.push_requested = False
        if self.provider is None or not self.repos:
            result.cursor["last_sync_at"] = iso(now_dt)
            return result
        etags: dict[str, str] = dict(result.cursor.get("etags") or {})
        last_ids: dict[str, str] = dict(result.cursor.get("last_event_ids") or {})
        errors: list[str] = []
        for repo in self.repos:
            try:
                items, etag = self.provider.events(repo, etag=etags.get(repo))
            except (ProviderError, PermissionError, ValueError) as exc:
                errors.append(f"{repo}: {str(exc)[:120]}")
                continue
            if etag:
                etags[repo] = etag
            if not items:
                continue
            newest = max((str(i.get("id") or "") for i in items), key=lambda v: int(v) if v.isdigit() else 0)
            baseline = repo not in last_ids and not self.settings.get("initial_import")
            floor = int(last_ids.get(repo) or 0)
            fresh = [i for i in items if str(i.get("id") or "").isdigit() and int(i["id"]) > floor]
            if not baseline:
                for item in sorted(fresh, key=lambda i: int(i["id"])):
                    event = normalise_api_event(item)
                    result.seen += 1
                    if event is None:
                        continue
                    keep, _ = wanted(event, self.settings)
                    if not keep:
                        continue
                    try:
                        outcome = self._ingest(self._signal(event, now_dt), now_dt)
                    except Exception as exc:  # one bad event never stops the sweep
                        log.warning("github ingest failed for %s: %s", item.get("id"), exc)
                        continue
                    if outcome == "added":
                        result.added += 1
                    elif outcome == "ignored":
                        result.ignored += 1
            if newest.isdigit():
                last_ids[repo] = newest
        result.cursor.update({"etags": etags, "last_event_ids": last_ids, "last_sync_at": iso(now_dt)})
        if errors:
            result.error = "; ".join(errors)[:300]
            result.health = {"status": "error", "error": result.error, "at": iso(now_dt), "webhook_events": self.webhook_events}
        else:
            result.health = {"status": "ok", "at": iso(now_dt), "seen": result.seen, "added": result.added, "webhook_events": self.webhook_events}
        return result

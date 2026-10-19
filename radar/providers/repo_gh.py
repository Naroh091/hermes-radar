"""Repository provider on the ``gh`` CLI (the owner's own GitHub login, read-only by default).

``events`` polls the public events API with an ETag so an unchanged repo costs one conditional
request (GitHub asks for ``X-Poll-Interval`` seconds between polls). Writes (``comment``) run only when
``allow_write`` is set, and the native operation layer gates them behind the owner's consent.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Callable

from .base import ProviderError, RepoProvider

Runner = Callable[[list[str], str | None], tuple[int, str, str]]
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _default_runner(timeout: float, binary: str) -> Runner:
    def run(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
        done = subprocess.run([binary, *argv], input=stdin, capture_output=True, text=True, timeout=timeout, env=env)
        return done.returncode, done.stdout, done.stderr

    return run


def split_http_response(raw: str) -> tuple[int, dict[str, str], str]:
    """``gh api -i`` output → (status, headers, body). Tolerates several header blocks (redirects)."""
    status, headers, rest = 0, {}, raw
    while rest.startswith("HTTP/"):
        head, _, rest = rest.partition("\n\n") if "\n\n" in rest else rest.partition("\r\n\r\n")
        lines = head.replace("\r", "").splitlines()
        try:
            status = int(lines[0].split()[1])
        except (IndexError, ValueError):
            status = 0
        headers = {}
        for line in lines[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
    return status, headers, rest


class GhRepoProvider(RepoProvider):
    provider_id = "gh"

    def __init__(self, *, runner: Runner | None = None, allow_write: bool = False, timeout: float = 25, binary: str = "gh"):
        self.binary = binary
        self.timeout = timeout
        self.read_only = not allow_write
        self._runner = runner

    @property
    def available(self) -> bool:
        return self._runner is not None or shutil.which(self.binary) is not None

    def _run(self, argv: list[str], *, stdin: str | None = None) -> tuple[int, str, str]:
        runner = self._runner or _default_runner(self.timeout, self.binary)
        try:
            return runner(argv, stdin)
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"gh {argv[0]} timed out after {self.timeout:.0f}s") from exc
        except OSError as exc:
            raise ProviderError(f"gh not runnable: {exc}") from exc

    @staticmethod
    def _check_repo(repo: str) -> str:
        if not _REPO.match(str(repo or "")):
            raise ValueError(f"invalid repository {repo!r}")
        return repo

    def events(self, repo: str, *, etag: str | None = None, per_page: int = 50) -> tuple[list[dict[str, Any]], str | None]:
        repo = self._check_repo(repo)
        argv = ["api", "-i", f"repos/{repo}/events?per_page={max(1, min(int(per_page), 100))}"]
        if etag:
            argv[2:2] = ["-H", f"If-None-Match: {etag}"]
        code, out, err = self._run(argv)
        status, headers, body = split_http_response(out)
        if status == 304:
            return [], etag
        if status and status >= 400 or (code != 0 and not body.strip()):
            raise ProviderError(f"gh api events {repo}: HTTP {status or code} {err.strip()[:200]}")
        try:
            items = json.loads(body) if body.strip() else []
        except ValueError as exc:
            raise ProviderError(f"gh api events {repo}: unreadable body") from exc
        return ([i for i in items if isinstance(i, dict)] if isinstance(items, list) else []), headers.get("etag") or etag

    def _json(self, argv: list[str], *, stdin: str | None = None) -> Any:
        code, out, err = self._run(argv, stdin=stdin)
        if code != 0:
            raise ProviderError(f"gh {' '.join(argv[:2])}: {err.strip()[:200] or out.strip()[:200]}")
        try:
            return json.loads(out) if out.strip() else {}
        except ValueError as exc:
            raise ProviderError("gh returned no JSON") from exc

    def issue(self, repo: str, number: int) -> dict[str, Any]:
        return self._json(["api", f"repos/{self._check_repo(repo)}/issues/{int(number)}"])

    def pull_request(self, repo: str, number: int) -> dict[str, Any]:
        return self._json(["api", f"repos/{self._check_repo(repo)}/pulls/{int(number)}"])

    def get_comment(self, repo: str, comment_id: int) -> dict[str, Any]:
        return self._json(["api", f"repos/{self._check_repo(repo)}/issues/comments/{int(comment_id)}"])

    def comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        """Post an issue/PR comment (external: visible to everyone on the repo)."""
        if self.read_only:
            raise PermissionError("github.comment is not permitted on this provider")
        if not str(body or "").strip():
            raise ValueError("comment body is required")
        payload = json.dumps({"body": body})
        data = self._json(["api", "-X", "POST", f"repos/{self._check_repo(repo)}/issues/{int(number)}/comments", "--input", "-"], stdin=payload)
        return {"id": data.get("id"), "url": data.get("html_url") or "", "created_at": data.get("created_at") or ""}

    def default_branch(self, repo: str) -> str:
        data = self._json(["api", f"repos/{self._check_repo(repo)}"])
        return str(data.get("default_branch") or "main")

    def pull_request_by_branch(self, repo: str, head: str) -> dict[str, Any] | None:
        """The open or closed PR whose head is ``head`` (``owner:branch`` accepted), or None."""
        branch = str(head or "").split(":")[-1]
        data = self._json(["pr", "list", "-R", self._check_repo(repo), "--head", branch, "--state", "all", "--limit", "1",
                           "--json", "number,url,isDraft,state,title"])
        items = data if isinstance(data, list) else []
        return dict(items[0]) if items and isinstance(items[0], dict) else None

    def open_pr(self, repo: str, *, head: str, base: str, title: str, body: str, draft: bool = True) -> dict[str, Any]:
        """Open a pull request, always as a draft (local: reversible, nobody is notified to merge)."""
        if self.read_only:
            raise PermissionError("github.open_pr is not permitted on this provider")
        if not draft:
            raise ValueError("pull requests opened by radar are always drafts")
        if not str(head or "").strip() or not str(title or "").strip():
            raise ValueError("head branch and title are required")
        argv = ["pr", "create", "-R", self._check_repo(repo), "--draft", "--head", head, "--base", base or "main", "--title", title[:250], "--body-file", "-"]
        code, out, err = self._run(argv, stdin=body)
        if code != 0:
            raise ProviderError(f"gh pr create: {err.strip()[:300] or out.strip()[:300]}")
        url = next((line.strip() for line in out.splitlines() if line.strip().startswith("https://")), out.strip()[:200])
        number = int(url.rstrip("/").rsplit("/", 1)[-1]) if url.rstrip("/").rsplit("/", 1)[-1].isdigit() else None
        return {"number": number, "url": url, "draft": True}

    def capabilities(self) -> dict[str, bool]:
        return {"events": True, "read": True, "comment": not self.read_only, "open_pr": not self.read_only}

"""Gmail ``users.watch`` renewal through the REST API with the mail MCP's own OAuth credentials.

The watch expires after seven days at most; without renewal push stops silently. The scheduler renews
every day for every push-enabled Gmail source and records the expiry in the source health.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


def load_credentials(credentials_dir: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    base = Path(credentials_dir).expanduser()
    creds = json.loads((base / "credentials.json").read_text(encoding="utf-8"))
    keys = json.loads((base / "gcp-oauth.keys.json").read_text(encoding="utf-8"))
    keys = keys.get("installed", keys.get("web", keys))
    tokens = creds.get("tokens", creds)
    return keys, tokens


def access_token(credentials_dir: str | Path, *, opener: Any = None) -> str:
    keys, tokens = load_credentials(credentials_dir)
    data = urllib.parse.urlencode(
        {
            "client_id": keys["client_id"], "client_secret": keys["client_secret"],
            "refresh_token": tokens["refresh_token"], "grant_type": "refresh_token",
        }
    ).encode()
    request = urllib.request.Request(
        keys.get("token_uri", "https://oauth2.googleapis.com/token"), data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with (opener or urllib.request.urlopen)(request, timeout=20) as response:
        return json.load(response)["access_token"]


def gmail_call(token: str, path: str, method: str = "GET", body: dict[str, Any] | None = None, *, opener: Any = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{GMAIL_API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        return {"error": exc.code, "detail": exc.read().decode(errors="replace")[:300]}


def renew_watch(credentials_dir: str | Path, topic: str, labels: list[str] | None = None, *, opener: Any = None) -> dict[str, Any]:
    if not topic:
        raise ValueError("a Pub/Sub topic is required to renew the Gmail watch")
    token = access_token(credentials_dir, opener=opener)
    body = {"topicName": topic, "labelIds": labels or ["INBOX"]}
    result = gmail_call(token, "/watch", "POST", body, opener=opener)
    if "historyId" not in result:
        raise RuntimeError(f"watch renewal failed: {result}")
    return {"history_id": str(result["historyId"]), "expiration_ms": int(result.get("expiration") or 0)}


def profile(credentials_dir: str | Path, *, opener: Any = None) -> dict[str, Any]:
    token = access_token(credentials_dir, opener=opener)
    return gmail_call(token, "/profile", opener=opener)

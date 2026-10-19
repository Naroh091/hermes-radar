"""Gmail push (Pub/Sub) receiver: verify, deduplicate, forward to the host webhook route.

It answers 200 as soon as the request is verified; forwarding runs in the background so Pub/Sub
never retries because of latency. It reads nothing from Gmail itself: the plugin syncs through the
configured mail provider when the forwarded event is consumed.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.request
from collections import OrderedDict
from typing import Any

log = logging.getLogger(__name__)

CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")


def b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64decode(data + b"=" * (-len(data) % 4))


def hermes_headers(secret: str, body: bytes, request_id: str = "") -> dict[str, str]:
    ts = str(int(time.time()))
    signature = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-Webhook-Timestamp": ts, "X-Webhook-Signature-V2": signature}
    if request_id:
        headers["X-Request-ID"] = request_id[:120]
    return headers


class JwtVerifier:
    def __init__(self, *, audiences: set[str], service_account_email: str = "", certs_url: str = CERTS_URL):
        self.audiences = {a for a in audiences if a}
        self.service_account_email = service_account_email
        self.certs_url = certs_url
        self._certs: dict[str, dict[str, Any]] = {}
        self._certs_at = 0.0

    def certs(self) -> dict[str, dict[str, Any]]:
        if time.time() - self._certs_at > 3600 or not self._certs:
            with urllib.request.urlopen(self.certs_url, timeout=10) as response:
                self._certs = {key["kid"]: key for key in json.load(response)["keys"]}
                self._certs_at = time.time()
        return self._certs

    def verify(self, token: str) -> dict[str, Any]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

        header, payload, signature = token.split(".")
        kid = json.loads(b64url(header.encode()))["kid"]
        key = self.certs()[kid]
        numbers = RSAPublicNumbers(int.from_bytes(b64url(key["e"].encode()), "big"), int.from_bytes(b64url(key["n"].encode()), "big"))
        numbers.public_key().verify(b64url(signature.encode()), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
        claims = json.loads(b64url(payload.encode()))
        if claims.get("iss") not in ISSUERS:
            raise ValueError("unexpected issuer")
        if not self.audiences or claims.get("aud") not in self.audiences:
            raise ValueError(f"unexpected audience {claims.get('aud')!r}")
        if float(claims.get("exp", 0)) <= time.time():
            raise ValueError("token expired")
        if self.service_account_email and claims.get("email") != self.service_account_email:
            raise ValueError("unexpected service account")
        return claims


class GmailPushReceiver:
    def __init__(
        self, *, token: str, public_url: str, hermes_webhook_url: str, hermes_secret: str,
        route: str = "gmail-push", expected_subscription: str = "", allowed_accounts: set[str] | None = None,
        service_account_email: str = "", extra_audiences: tuple[str, ...] = (), require_oidc: bool = True,
        forward: Any = None,
    ):
        if not token or not hermes_secret:
            raise ValueError("push token and hermes secret are required")
        self.token = token
        self.route = route
        self.hermes_webhook_url = hermes_webhook_url
        self.hermes_secret = hermes_secret
        self.expected_subscription = expected_subscription
        self.allowed_accounts = {a.lower() for a in (allowed_accounts or set())}
        self.require_oidc = require_oidc
        audiences = {public_url, f"{public_url}?token={token}", *extra_audiences} if public_url else set(extra_audiences)
        self.verifier = JwtVerifier(audiences=audiences, service_account_email=service_account_email)
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._forward = forward
        self.stats = {"accepted": 0, "duplicates": 0, "rejected": 0, "forwarded": 0, "forward_errors": 0}
        self._runner: Any = None

    # -- verification ---------------------------------------------------------------------
    def _remember(self, message_id: str) -> bool:
        if not message_id:
            return True
        if message_id in self._seen:
            return False
        self._seen[message_id] = time.time()
        while len(self._seen) > 5000:
            self._seen.popitem(last=False)
        return True

    def decode(self, body: dict[str, Any]) -> dict[str, Any]:
        message = body.get("message") or {}
        if self.expected_subscription and body.get("subscription") != self.expected_subscription:
            raise PermissionError("bad subscription")
        try:
            info = json.loads(base64.b64decode(message.get("data", "")))
        except Exception as exc:
            raise ValueError("bad data") from exc
        address = str(info.get("emailAddress") or "").lower()
        if self.allowed_accounts and address not in self.allowed_accounts:
            raise PermissionError("bad sender")
        return {
            "emailAddress": address, "historyId": str(info.get("historyId") or ""),
            "pubsubMessageId": str(message.get("messageId") or ""), "publishTime": str(message.get("publishTime") or ""),
        }

    async def forward(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        headers = hermes_headers(self.hermes_secret, body, payload.get("pubsubMessageId", ""))
        try:
            if self._forward is not None:
                await self._forward(self.hermes_webhook_url, body, headers)
            else:
                from aiohttp import ClientSession
                async with ClientSession() as session:
                    async with session.post(self.hermes_webhook_url, data=body, headers=headers, timeout=20) as response:
                        if response.status >= 300:
                            raise RuntimeError(f"hermes webhook returned {response.status}")
            self.stats["forwarded"] += 1
        except Exception as exc:
            self.stats["forward_errors"] += 1
            log.warning("gmail push forward failed: %s", exc)

    # -- aiohttp ------------------------------------------------------------------------------
    def build_app(self) -> Any:
        from aiohttp import web

        async def health(_request: Any) -> Any:
            return web.json_response({"status": "ok", "service": "radar-gmail-push", **self.stats})

        async def receive(request: Any) -> Any:
            if request.path not in ("/", f"/{self.route}", "/ingress/gmail"):
                return web.json_response({"error": "no such route"}, status=404)
            if request.query.get("token") != self.token:
                self.stats["rejected"] += 1
                return web.json_response({"error": "bad token"}, status=403)
            auth = request.headers.get("Authorization", "")
            if self.require_oidc:
                if not auth.startswith("Bearer "):
                    self.stats["rejected"] += 1
                    return web.json_response({"error": "missing jwt"}, status=403)
                try:
                    await asyncio.to_thread(self.verifier.verify, auth[7:])
                except Exception as exc:
                    self.stats["rejected"] += 1
                    log.warning("gmail push jwt rejected: %s", exc)
                    return web.json_response({"error": "bad jwt"}, status=403)
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "invalid JSON"}, status=400)
            try:
                payload = self.decode(body if isinstance(body, dict) else {})
            except PermissionError as exc:
                self.stats["rejected"] += 1
                return web.json_response({"error": str(exc)}, status=403)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            if not self._remember(payload["pubsubMessageId"]):
                self.stats["duplicates"] += 1
                return web.json_response({"ok": True, "dedup": True})
            self.stats["accepted"] += 1
            asyncio.create_task(self.forward(payload))
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_get("/healthz", health)
        app.router.add_post("/{tail:.*}", receive)
        return app

    async def start(self, host: str = "127.0.0.1", port: int = 8666) -> None:
        from aiohttp import web

        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        log.info("radar gmail push receiver listening on %s:%s", host, port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

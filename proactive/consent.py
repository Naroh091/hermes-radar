from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import canonical_json


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class ConsentClaims:
    proposal_id: str
    plan_hash: str
    actor_ref: str
    destination_id: str
    expires_at: str
    nonce: str


class ConsentSigner:
    """HMAC tokens bind one proposal, plan, actor, destination, expiry, and nonce."""

    def __init__(self, key: bytes):
        if len(key) < 32:
            raise ValueError("consent signing key must contain at least 32 bytes")
        self._key = key

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "ConsentSigner":
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.exists():
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, secrets.token_bytes(32))
            finally:
                os.close(fd)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return cls(target.read_bytes())

    def issue(
        self, *, proposal_id: str, plan_hash: str, actor_ref: str,
        destination_id: str, expires_at: str,
    ) -> str:
        payload = {
            "v": 1,
            "proposal_id": proposal_id,
            "plan_hash": plan_hash,
            "actor_ref": actor_ref,
            "destination_id": destination_id,
            "expires_at": expires_at,
            "nonce": secrets.token_urlsafe(18),
        }
        encoded = _b64(canonical_json(payload).encode("utf-8"))
        signature = _b64(hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(self, token: str) -> ConsentClaims:
        try:
            encoded, supplied = token.split(".", 1)
            expected = _b64(hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("invalid consent signature")
            payload: Any = json.loads(_unb64(encoded))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("invalid consent token") from exc
        expected_keys = {
            "v", "proposal_id", "plan_hash", "actor_ref", "destination_id", "expires_at", "nonce"
        }
        if not isinstance(payload, dict) or set(payload) != expected_keys or payload.get("v") != 1:
            raise ValueError("invalid consent claims")
        values = [payload[key] for key in expected_keys - {"v"}]
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("invalid consent claims")
        return ConsentClaims(
            proposal_id=payload["proposal_id"], plan_hash=payload["plan_hash"],
            actor_ref=payload["actor_ref"], destination_id=payload["destination_id"],
            expires_at=payload["expires_at"], nonce=payload["nonce"],
        )

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

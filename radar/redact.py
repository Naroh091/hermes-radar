"""Mask secret-looking strings before they are stored or shown to a model.

Owners paste tokens into their chats. Radar keeps turn excerpts for weeks and feeds them to models, so
anything shaped like a credential is replaced before it reaches the store. The patterns favour recall
over precision: a masked commit hash costs nothing, a stored API key does.
"""
from __future__ import annotations

import re

MASK = "[redacted]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),  # JWT
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), MASK),  # Meta Graph tokens
    (re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"), MASK),  # Google OAuth access tokens
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), MASK),  # Google API keys
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),  # AWS access key ids
    (re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}"), MASK),  # OpenAI, Anthropic, Stripe
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), MASK),  # GitHub
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), MASK),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"), MASK),  # GitLab
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),  # Slack
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), MASK),  # Telegram bot tokens
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(token|secret|password|passwd|contraseña|clave|api[_ -]?key|access[_ -]?key|auth[_ -]?key)\b(\s*[:=]\s*)[\"']?([^\s\"']{8,})"), r"\1\2" + MASK),
    (re.compile(r"\b[A-Za-z0-9_-]{40,}\b"), MASK),  # long opaque blobs (checked for mixed classes below)
]
_HEX = re.compile(r"^[0-9a-f]+$")
_URLISH = re.compile(r"^(?:https?|ftp)://", re.I)


def _opaque(token: str) -> bool:
    """Long strings that mix letters and digits and are not a lowercase hex digest (git SHAs stay)."""
    if _HEX.match(token):
        return False
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    has_digit = any(c.isdigit() for c in token)
    return has_digit and (has_upper and has_lower or len(token) >= 60)


def redact(text: str | None) -> str:
    """Return ``text`` with credential-shaped substrings replaced by :data:`MASK`."""
    if not text:
        return "" if text is None else text
    out = str(text)
    for pattern, replacement in _PATTERNS[:-1]:
        out = pattern.sub(replacement, out)
    long_blob = _PATTERNS[-1][0]
    return long_blob.sub(lambda m: MASK if _opaque(m.group(0)) else m.group(0), out)


def redact_fields(values: dict, keys: tuple[str, ...]) -> dict:
    """Copy of ``values`` with the named string fields redacted."""
    out = dict(values)
    for key in keys:
        if isinstance(out.get(key), str):
            out[key] = redact(out[key])
    return out

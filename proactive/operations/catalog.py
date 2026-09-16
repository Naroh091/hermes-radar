from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class OperationResult:
    status: str
    data: dict


class OperationSpec:
    id = ""
    version = ""
    effect_class = "unknown"

    def execute(self, scope, parameters: dict, context=None) -> OperationResult:
        raise NotImplementedError


class HttpStatusCheck(OperationSpec):
    id, version, effect_class = "http_status.check", "1", "informational"

    @staticmethod
    def _validate(url: str, allowlist: list[str]) -> tuple[str, str, int | None, str]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("only HTTPS URLs without credentials/fragments are allowed")
        if parsed.hostname is None or parsed.hostname.lower() not in {h.lower() for h in allowlist}:
            raise ValueError("host is not in the configured HTTPS allowlist")
        if parsed.port not in (None, 443): raise ValueError("only HTTPS port 443 is allowed")
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        except OSError as exc: raise ValueError("host cannot be resolved") from exc
        for _, _, _, _, sockaddr in addresses:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValueError("private or local network target is forbidden")
        return parsed.hostname, parsed.path or "/", parsed.port, url

    def execute(self, scope, parameters: dict, context=None) -> OperationResult:
        url = parameters.get("url") if isinstance(parameters, dict) else None
        allowlist = (context or {}).get("allowlist", [])
        if not isinstance(url, str) or not isinstance(allowlist, list): return OperationResult("failed", {"error": "invalid parameters"})
        try:
            self._validate(url, allowlist)
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "hermes-proactive/0.1"})
            with urllib.request.urlopen(req, timeout=min(int(parameters.get("timeout_seconds", 10)), 30)) as response:
                body = response.read(4097)
                return OperationResult("succeeded", {"http_status": response.status, "bytes_sampled": len(body), "truncated": len(body) > 4096})
        except urllib.error.HTTPError as exc:
            return OperationResult("succeeded", {"http_status": exc.code, "bytes_sampled": 0})
        except Exception as exc:
            return OperationResult("failed", {"error": str(exc)})


OPERATIONS = {HttpStatusCheck.id: HttpStatusCheck()}

def operation_catalog() -> list[dict]:
    return [{"id": op.id, "version": op.version, "effect_class": op.effect_class} for op in OPERATIONS.values()]

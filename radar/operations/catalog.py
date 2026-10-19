from __future__ import annotations

import ipaddress
import http.client
import socket
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class OperationResult:
    status: str
    data: dict[str, Any]


class OperationSpec:
    id = ""
    version = ""
    effect_class = "external"
    input_schema: dict[str, Any] = {"type": "object", "additionalProperties": False}
    result_schema: dict[str, Any] = {"type": "object", "additionalProperties": False}
    allowed_resource_types: tuple[str, ...] = ()
    required_grants: tuple[str, ...] = ()
    timeout_seconds = 10
    maximum_response_bytes = 4096
    budget_units = 1
    supports_idempotency = False
    supports_reconciliation = False

    def execute(self, scope, parameters: dict[str, Any], context=None) -> OperationResult:
        raise NotImplementedError

    def validate_parameters(self, parameters: Any) -> None:
        if not isinstance(parameters, dict):
            raise ValueError("parameters must be an object")

    def resource_identity(self, parameters: dict[str, Any]) -> tuple[str, str] | None:
        return None

    def reconcile(self, *args, **kwargs) -> str:
        return "unknown"

    def public_metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "effect_class": self.effect_class,
            "allowed_resource_types": list(self.allowed_resource_types),
            "required_grants": list(self.required_grants),
            "timeout_seconds": self.timeout_seconds,
            "maximum_response_bytes": self.maximum_response_bytes,
            "budget_units": self.budget_units,
            "supports_idempotency": self.supports_idempotency,
            "supports_reconciliation": self.supports_reconciliation,
        }


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection pinned to a validated IP while retaining hostname verification."""

    def __init__(self, hostname: str, ip: str, *, timeout: int) -> None:
        super().__init__(hostname, 443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class HttpStatusCheck(OperationSpec):
    id = "http_status.check"
    version = "1"
    effect_class = "read"
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
        },
    }
    result_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["http_status", "bytes_sampled"],
        "properties": {
            "http_status": {"type": "integer", "minimum": 100, "maximum": 599},
            "bytes_sampled": {"type": "integer", "minimum": 0},
            "truncated": {"type": "boolean"},
        },
    }
    allowed_resource_types = ("https_endpoint",)
    required_grants = ("read",)
    timeout_seconds = 10
    maximum_response_bytes = 4096
    budget_units = 1
    supports_idempotency = True
    supports_reconciliation = False

    @staticmethod
    def validate_parameters(parameters: Any) -> tuple[str, int]:
        if not isinstance(parameters, dict):
            raise ValueError("parameters must be an object")
        unknown = sorted(set(parameters) - {"url", "timeout_seconds"})
        if unknown:
            raise ValueError("unknown parameters: " + ", ".join(unknown))
        url = parameters.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("url is required")
        timeout = parameters.get("timeout_seconds", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 30:
            raise ValueError("timeout_seconds must be an integer between 1 and 30")
        return url, timeout

    def resource_identity(self, parameters: dict[str, Any]) -> tuple[str, str]:
        url, _ = self.validate_parameters(parameters)
        hostname = urlsplit(url).hostname
        if hostname is None:
            raise ValueError("url must contain a host")
        return "host", hostname.lower().rstrip(".")

    @staticmethod
    def _validate_target(url: str, allowlist: list[str]) -> tuple[Any, list[str]]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("only HTTPS URLs without credentials/fragments are allowed")
        normalized_allowlist = {host.lower().rstrip(".") for host in allowlist if isinstance(host, str)}
        hostname = parsed.hostname.lower().rstrip(".") if parsed.hostname else None
        if hostname is None or hostname not in normalized_allowlist:
            raise ValueError("host is not in the configured HTTPS allowlist")
        if parsed.port not in (None, 443):
            raise ValueError("only HTTPS port 443 is allowed")
        try:
            addresses = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("host cannot be resolved") from exc
        if not addresses:
            raise ValueError("host cannot be resolved")
        public_addresses: list[str] = []
        for _, _, _, _, sockaddr in addresses:
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                not ip.is_global
                or ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                raise ValueError("private or local network target is forbidden")
            normalized_ip = str(ip)
            if normalized_ip not in public_addresses:
                public_addresses.append(normalized_ip)
        return parsed, public_addresses

    def execute(self, scope, parameters: dict[str, Any], context=None) -> OperationResult:
        allowlist = (context or {}).get("allowlist", [])
        if not isinstance(allowlist, list):
            return OperationResult("failed", {"error": "allowlist must be an array"})
        try:
            url, timeout = self.validate_parameters(parameters)
            parsed, addresses = self._validate_target(url, allowlist)
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            last_error: Exception | None = None
            for address in addresses:
                connection = _PinnedHTTPSConnection(parsed.hostname, address, timeout=timeout)
                try:
                    connection.request(
                        "GET", target,
                        headers={"Host": parsed.hostname, "User-Agent": "hermes-radar/0.3"},
                    )
                    response = connection.getresponse()
                    body = response.read(self.maximum_response_bytes + 1)
                    return OperationResult(
                        "succeeded",
                        {
                            "http_status": response.status,
                            "bytes_sampled": min(len(body), self.maximum_response_bytes),
                            "truncated": len(body) > self.maximum_response_bytes,
                        },
                    )
                except Exception as exc:
                    last_error = exc
                finally:
                    connection.close()
            if last_error is not None:
                raise last_error
            raise RuntimeError("host has no validated addresses")
        except Exception as exc:
            return OperationResult("failed", {"error": str(exc)})


OPERATIONS = {HttpStatusCheck.id: HttpStatusCheck()}


def operation_catalog() -> list[dict[str, Any]]:
    return [operation.public_metadata() for operation in OPERATIONS.values()]

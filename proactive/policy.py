from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .operations import OPERATIONS
from .store import Store


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    code: str
    permission_revision: int | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    grant_id: str | None = None


class Policy:
    """Deterministic authorization. Model output never reaches this boundary as authority."""

    def __init__(self, store: Store, allowed_operations: Iterable[str]):
        self.store = store
        self.allowed_operations = frozenset(allowed_operations)

    @staticmethod
    def _scope_allows(resource_type: str, resource_value: str, resource_scope: dict[str, Any]) -> bool:
        if resource_type != "host":
            return False
        hosts = resource_scope.get("hosts")
        if not isinstance(hosts, list):
            return False
        normalized = {
            host.lower().rstrip(".")
            for host in hosts
            if isinstance(host, str) and host.strip()
        }
        return resource_value in normalized

    def authorize_informational(
        self,
        operation_id: str,
        source_id: str,
        parameters: dict[str, Any],
        now: str,
    ) -> AuthorizationDecision:
        operation = OPERATIONS.get(operation_id)
        if operation is None:
            return AuthorizationDecision(False, "unknown_operation")
        if operation.effect_class != "informational":
            return AuthorizationDecision(False, "consent_required")
        if operation_id not in self.allowed_operations:
            return AuthorizationDecision(False, "operation_not_enabled")
        try:
            operation.validate_parameters(parameters)
            resource = operation.resource_identity(parameters)
        except (TypeError, ValueError):
            return AuthorizationDecision(False, "invalid_parameters")
        if resource is None:
            return AuthorizationDecision(False, "resource_unverifiable")
        resource_type, resource_value = resource
        grants = self.store.active_operation_grants(operation_id, source_id, now)
        if not grants:
            return AuthorizationDecision(False, "grant_missing_or_inactive")
        for grant in grants:
            if not self._scope_allows(resource_type, resource_value, grant["source_resource_scope"]):
                continue
            if not self._scope_allows(resource_type, resource_value, grant["resource_scope"]):
                continue
            return AuthorizationDecision(
                True,
                "authorized",
                permission_revision=grant["permission_revision"],
                limits=grant["limits"],
                grant_id=grant["grant_id"],
            )
        return AuthorizationDecision(False, "resource_not_granted")

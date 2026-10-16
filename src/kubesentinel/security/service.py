"""Orchestrates inventory -> engine -> persistence for the security &
configuration auditor. The one entry point the API/CLI call through, and
the only module in this package with side effects (cluster reads +
persistence) -- mirrors `network/service.py::NetworkService`'s own shape.

No incident linking here, unlike `NetworkService`/`SchedulingService`/
`OptimizationService`: a security/config finding (an RBAC grant, a
missing NetworkPolicy, a missing PDB) has no single owning incident the
way a Pod-level resource problem does -- most of this engine's own rule
families (RBAC, NetworkPolicy coverage, PDB coverage) describe a
cluster-scoped or namespace-scoped fact, not one Pod's own story.
"""

from __future__ import annotations

from kubesentinel.config import Settings
from kubesentinel.metrics.registry import security_audit_total, security_findings_total
from kubesentinel.models.security import SecurityAuditResult, SecurityFinding
from kubesentinel.observer.client import ClusterClients
from kubesentinel.persistence.interface import SecurityRepository
from kubesentinel.security.engine import SecurityAuditEngine
from kubesentinel.security.inventory import build_security_inventory


class FindingNotFoundError(Exception):
    pass


class SecurityService:
    def __init__(self, clients: ClusterClients, repo: SecurityRepository, settings: Settings):
        self._clients = clients
        self._repo = repo
        self._engine = SecurityAuditEngine(settings)

    def audit(self, namespaces: list[str] | None = None) -> SecurityAuditResult:
        namespaces = namespaces or []
        try:
            inventory = build_security_inventory(self._clients, namespaces=namespaces)
            result = self._engine.audit(inventory, namespaces)
        except Exception:  # noqa: BLE001 - any failure here means "failed", not a partial result
            security_audit_total.labels(result="failed").inc()
            raise

        self._repo.save_findings(result.findings)
        for finding in result.findings:
            security_findings_total.labels(severity=finding.severity.value.lower()).inc()
        security_audit_total.labels(result="partial" if result.warnings else "success").inc()
        return result

    def get_finding(self, finding_id: str) -> SecurityFinding:
        finding = self._repo.get_finding(finding_id)
        if finding is None:
            raise FindingNotFoundError(f"no security finding {finding_id}")
        return finding

    def list_findings(
        self, *, namespace: str | None = None, severity: str | None = None, rule_id: str | None = None
    ) -> list[SecurityFinding]:
        return self._repo.list_findings(namespace=namespace, severity=severity, rule_id=rule_id)

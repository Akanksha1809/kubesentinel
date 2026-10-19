"""Runs every registered security rule against a `SecurityInventory` and
collects the findings. Mirrors `diagnostics/engine.py::DiagnosticEngine`
exactly: a list of rule objects, each a pure function, evaluated
uniformly -- applied to a new domain.

Unlike `DiagnosticEngine`'s static `DEFAULT_RULES` list (none of its
rules need configuration), two of this engine's rules need `Settings`
(the built-in-Role exclusion list, the PDB-coverage replica threshold) --
so `SecurityAuditEngine` builds its own rules list per instance, the same
settings-threading shape `OptimizationService` already uses for its own
pure functions, rather than a module-level singleton list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from kubesentinel.config import Settings
from kubesentinel.models.completeness import EvidenceCompleteness
from kubesentinel.models.security import SecurityAuditResult
from kubesentinel.security.inventory import SecurityInventory
from kubesentinel.security.rules.availability import MissingPdbForMultiReplicaWorkloadRule
from kubesentinel.security.rules.base import SecurityRule
from kubesentinel.security.rules.container import (
    AllowPrivilegeEscalationRule,
    HostNamespaceSharingRule,
    HostPathVolumeRule,
    MissingReadOnlyRootFsRule,
    MutableImageTagRule,
    PrivilegedContainerRule,
    RunAsRootRule,
    SecretAsEnvVarRule,
)
from kubesentinel.security.rules.network_policy import NamespaceWithoutNetworkPolicyRule, UnrestrictedIngressPolicyRule
from kubesentinel.security.rules.pod_security import NamespaceWithoutPodSecurityStandardRule
from kubesentinel.security.rules.rbac import ClusterAdminBindingRule, EffectiveRbacEscalationRule, WildcardRbacRuleRule


def default_rules(settings: Settings) -> list[SecurityRule]:
    return [
        PrivilegedContainerRule(),
        AllowPrivilegeEscalationRule(),
        RunAsRootRule(),
        MissingReadOnlyRootFsRule(),
        HostNamespaceSharingRule(),
        HostPathVolumeRule(),
        MutableImageTagRule(),
        SecretAsEnvVarRule(),
        ClusterAdminBindingRule(settings),
        WildcardRbacRuleRule(settings),
        EffectiveRbacEscalationRule(settings),
        NamespaceWithoutNetworkPolicyRule(settings),
        UnrestrictedIngressPolicyRule(),
        MissingPdbForMultiReplicaWorkloadRule(settings),
        NamespaceWithoutPodSecurityStandardRule(settings),
    ]


class SecurityAuditEngine:
    def __init__(self, settings: Settings, rules: list[SecurityRule] | None = None):
        self._rules = rules if rules is not None else default_rules(settings)

    def audit(self, inventory: SecurityInventory, namespaces: list[str]) -> SecurityAuditResult:
        all_findings = []
        for rule in self._rules:
            all_findings.extend(rule.evaluate(inventory))

        warnings = list(inventory.warnings)
        return SecurityAuditResult(
            audit_id=str(uuid4()),
            analyzed_at=datetime.now(timezone.utc),
            namespaces=namespaces,
            findings=all_findings,
            rules_evaluated=[rule.rule_id for rule in self._rules],
            warnings=warnings,
            # The shared vocabulary (models/completeness.py): a non-empty
            # `warnings` here always means at least one rule family's own
            # inventory read failed this pass (never populated for any
            # other reason), so DEGRADED is the correct read whenever it
            # is non-empty, never a separate judgment call.
            evidence_completeness=EvidenceCompleteness.DEGRADED if warnings else EvidenceCompleteness.COMPLETE,
        )

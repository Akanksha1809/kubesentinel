"""NetworkPolicy coverage rules (docs/SECURITY_AUDITING.md Section 3.3).
Reuses `network/policy_inventory.py`'s own static-analysis classification
(`has_unrestricted_ingress_rule`/`summarize_policy`) rather than re-reading
and re-interpreting NetworkPolicy objects a second time, per the platform
spec's own instruction.

Both rules carry the exact same kindnet-non-enforcement caveat Milestone 4
already established: a configuration-gap finding, never a claim about
actually-observed traffic -- see docs/NETWORK_DIAGNOSTICS.md Section 2/11.
"""

from __future__ import annotations

from uuid import uuid4

from kubesentinel.config import Settings
from kubesentinel.models.security import SecurityFinding, SecurityResourceRef, SecuritySeverity
from kubesentinel.network.policy_inventory import has_unrestricted_ingress_rule, summarize_policy
from kubesentinel.security.inventory import SecurityInventory
from kubesentinel.security.rules.base import SecurityRule

_ENFORCEMENT_CAVEAT = (
    "This is static analysis of the policy object only -- it does not confirm whether this "
    "cluster's own CNI actually enforces NetworkPolicy (kindnet, this lab's default CNI, does not)."
)


class NamespaceWithoutNetworkPolicyRule(SecurityRule):
    rule_id = "NAMESPACE_WITHOUT_NETWORK_POLICY"

    def __init__(self, settings: Settings):
        self._excluded_namespaces = set(settings.optimization_excluded_namespace_list)

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for namespace in sorted(inventory.namespaces_with_pods):
            if namespace in self._excluded_namespaces:
                continue
            policies = inventory.network_policies_by_namespace.get(namespace, [])
            if policies:
                continue
            findings.append(
                SecurityFinding(
                    finding_id=str(uuid4()),
                    rule_id=self.rule_id,
                    severity=SecuritySeverity.MEDIUM,
                    resource=SecurityResourceRef(namespace=namespace, kind="Namespace", name=namespace),
                    evidence=[f"namespace '{namespace}' has running Pods and zero NetworkPolicy objects"],
                    explanation=f"Without any NetworkPolicy, every Pod in this namespace accepts traffic "
                    f"from anywhere by default (on a CNI that enforces NetworkPolicy). {_ENFORCEMENT_CAVEAT}",
                    remediation="Add at least a default-deny NetworkPolicy, then explicitly allow the "
                    "traffic each workload genuinely needs.",
                )
            )
        return findings


class UnrestrictedIngressPolicyRule(SecurityRule):
    rule_id = "UNRESTRICTED_INGRESS_POLICY"

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for namespace, policies in inventory.network_policies_by_namespace.items():
            for policy in policies:
                if not has_unrestricted_ingress_rule(policy):
                    continue
                findings.append(
                    SecurityFinding(
                        finding_id=str(uuid4()),
                        rule_id=self.rule_id,
                        severity=SecuritySeverity.MEDIUM,
                        resource=SecurityResourceRef(namespace=namespace, kind="NetworkPolicy", name=policy.metadata.name),
                        evidence=[summarize_policy(policy)],
                        explanation=f"This NetworkPolicy has an Ingress rule with no 'from'/'ports' "
                        f"restriction at all -- an effective allow-all despite the policy existing. "
                        f"{_ENFORCEMENT_CAVEAT}",
                        remediation="Scope the ingress rule to specific sources (podSelector/namespaceSelector/"
                        "ipBlock) and/or ports, or remove the rule if it was meant to deny by default.",
                    )
                )
        return findings

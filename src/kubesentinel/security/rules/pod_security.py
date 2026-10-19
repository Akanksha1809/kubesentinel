"""Pod Security Admission namespace-label coverage
(docs/SECURITY_AUDITING.md's own previously-named gap: "Pod Security
Admission namespace labels | Not built, deliberately -- redundant with
the already-built, more granular container-level checks -- a candidate
for a later pass, not forgotten.")

That framing undersold the gap: the container-level rules in
`rules/container.py` (`PRIVILEGED_CONTAINER`, `RUN_AS_ROOT`, ...) are
*detective* -- they confirm a problem that already exists. Pod Security
Admission is *preventive* -- a namespace with
`pod-security.kubernetes.io/enforce` set rejects an unsafe Pod spec at
admission time, before it is ever created at all. The two are
complementary layers, not duplicates: a namespace can fail every
detective check this engine already has while also having zero
preventive coverage, and this rule is the only thing in this engine
that would ever say so.

Same reasoning Milestone 6's own `NamespaceWithoutNetworkPolicyRule`
established for its own namespace-level gap, applied to a different
admission mechanism.
"""

from __future__ import annotations

from uuid import uuid4

from kubesentinel.config import Settings
from kubesentinel.models.security import SecurityFinding, SecurityResourceRef, SecuritySeverity
from kubesentinel.security.inventory import SecurityInventory
from kubesentinel.security.rules.base import SecurityRule

_PSA_LABEL_KEYS = (
    "pod-security.kubernetes.io/enforce",
    "pod-security.kubernetes.io/audit",
    "pod-security.kubernetes.io/warn",
)


class NamespaceWithoutPodSecurityStandardRule(SecurityRule):
    rule_id = "NAMESPACE_WITHOUT_POD_SECURITY_STANDARD"

    def __init__(self, settings: Settings):
        # Same exclusion list `NamespaceWithoutNetworkPolicyRule` already
        # reuses from the optimization engine's own settings -- this
        # project's one shared "these namespaces aren't the user's to
        # fix" list, not a second, possibly-diverging copy of it.
        self._excluded_namespaces = set(settings.optimization_excluded_namespace_list)

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        # If the one Namespace LIST call itself failed this pass,
        # `namespace_labels` is entirely empty -- never treated as
        # "confirmed no labels on every namespace" (see
        # `SecurityInventory.namespace_labels`'s own docstring). Silently
        # producing zero findings here is the same "sub-read failure
        # degrades this rule family to empty, never a fabricated
        # finding" discipline every other rule in this engine already
        # follows.
        findings = []
        for namespace in sorted(inventory.namespaces_with_pods):
            if namespace in self._excluded_namespaces:
                continue
            labels = inventory.namespace_labels.get(namespace)
            if labels is None:
                continue
            if any(key in labels for key in _PSA_LABEL_KEYS):
                continue
            findings.append(
                SecurityFinding(
                    finding_id=str(uuid4()),
                    rule_id=self.rule_id,
                    severity=SecuritySeverity.LOW,
                    resource=SecurityResourceRef(namespace=namespace, kind="Namespace", name=namespace),
                    evidence=[
                        f"namespace '{namespace}' has running Pods and none of "
                        f"{', '.join(_PSA_LABEL_KEYS)} is set"
                    ],
                    explanation=(
                        "Without a Pod Security Admission label, nothing prevents an unsafe Pod spec "
                        "(privileged, running as root, allowing privilege escalation, ...) from being "
                        "created in this namespace at all -- every container-security finding this "
                        "engine already produces is detective, confirming a problem after the fact, "
                        "never preventive."
                    ),
                    remediation=(
                        "Add pod-security.kubernetes.io/enforce=baseline (or restricted, for a "
                        "stricter standard) to this namespace, or explicitly decide and document why "
                        "it stays unset."
                    ),
                )
            )
        return findings

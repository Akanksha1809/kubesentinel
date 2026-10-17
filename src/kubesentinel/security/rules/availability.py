"""PodDisruptionBudget coverage rule (docs/SECURITY_AUDITING.md Section
3.4) -- the roadmap's own named optional item, included since it's a
simple, standard, valuable check.
"""

from __future__ import annotations

from uuid import uuid4

from kubesentinel.config import Settings
from kubesentinel.models.security import SecurityFinding, SecurityResourceRef, SecuritySeverity
from kubesentinel.scheduling.constraints import label_selector_matches
from kubesentinel.security.inventory import SecurityInventory
from kubesentinel.security.rules.base import SecurityRule


class MissingPdbForMultiReplicaWorkloadRule(SecurityRule):
    rule_id = "MISSING_PDB_FOR_MULTI_REPLICA_WORKLOAD"

    def __init__(self, settings: Settings):
        self._min_replicas = settings.security_min_replicas_for_pdb_check

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for kind, workloads in (("Deployment", inventory.deployments), ("StatefulSet", inventory.stateful_sets)):
            for workload in workloads:
                desired = workload.spec.replicas or 0
                if desired < self._min_replicas:
                    continue
                labels = (workload.spec.template.metadata.labels or {}) if workload.spec.template.metadata else {}
                namespace = workload.metadata.namespace
                covered = any(
                    pdb.metadata.namespace == namespace and label_selector_matches(pdb.spec.selector, labels)
                    for pdb in inventory.pod_disruption_budgets
                )
                if covered:
                    continue
                findings.append(
                    SecurityFinding(
                        finding_id=str(uuid4()),
                        rule_id=self.rule_id,
                        severity=SecuritySeverity.LOW,
                        resource=SecurityResourceRef(namespace=namespace, kind=kind, name=workload.metadata.name),
                        workload_kind=kind,
                        workload_name=workload.metadata.name,
                        evidence=[f"{kind} '{workload.metadata.name}' has {desired} desired replicas and no matching PodDisruptionBudget"],
                        explanation="Without a PodDisruptionBudget, a voluntary disruption (node drain, "
                        "cluster upgrade) could evict every replica of this workload at once, with "
                        "nothing telling the eviction controller to leave some running.",
                        remediation="Add a PodDisruptionBudget selecting this workload's Pods with a "
                        "`minAvailable`/`maxUnavailable` appropriate for it.",
                    )
                )
        return findings

"""RBAC over-permissiveness rules (docs/SECURITY_AUDITING.md Section
3.2). Both rules deliberately exclude Kubernetes' own built-in,
intentionally-broad objects -- there is no fix a cluster operator can
apply to a built-in Role/binding, so flagging them would be pure noise
with no actionable remediation.
"""

from __future__ import annotations

from uuid import uuid4

from kubesentinel.config import Settings
from kubesentinel.models.security import SecurityFinding, SecurityResourceRef, SecuritySeverity
from kubesentinel.security.inventory import SecurityInventory
from kubesentinel.security.rules.base import SecurityRule

_CLUSTER_ADMIN_ROLE_NAME = "cluster-admin"


class ClusterAdminBindingRule(SecurityRule):
    rule_id = "CLUSTER_ADMIN_BINDING"

    def __init__(self, settings: Settings):
        # The cluster's own bootstrap bindings grant cluster-admin to a
        # Group, never a specific ServiceAccount/User -- matched by
        # subject kind+name-prefix, never by binding name (a binding could
        # be renamed without changing what it actually does). Found live,
        # not assumed: a real kind cluster has *two* such built-in Group
        # bindings, not one -- `system:masters` (the universal Kubernetes
        # bootstrap identity) and `kubeadm:cluster-admins` (kubeadm's own,
        # used by admin.conf) -- confirmed by reading this project's own
        # lab cluster's real ClusterRoleBindings rather than assuming only
        # the first, better-known one existed.
        self._builtin_group_prefixes = tuple(settings.security_builtin_subject_group_prefix_list)

    def _is_builtin_subject(self, subject) -> bool:
        return subject.kind == "Group" and subject.name.startswith(self._builtin_group_prefixes)

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for binding in inventory.cluster_role_bindings:
            if binding.role_ref.kind != "ClusterRole" or binding.role_ref.name != _CLUSTER_ADMIN_ROLE_NAME:
                continue
            for subject in binding.subjects or []:
                if self._is_builtin_subject(subject):
                    continue
                findings.append(
                    SecurityFinding(
                        finding_id=str(uuid4()),
                        rule_id=self.rule_id,
                        severity=SecuritySeverity.CRITICAL,
                        resource=SecurityResourceRef(namespace=None, kind="ClusterRoleBinding", name=binding.metadata.name),
                        evidence=[
                            f"ClusterRoleBinding '{binding.metadata.name}' grants 'cluster-admin' to "
                            f"{subject.kind} '{subject.name}'"
                            + (f" (namespace '{subject.namespace}')" if subject.namespace else "")
                        ],
                        explanation="cluster-admin grants unrestricted access to every resource in every "
                        "namespace -- the broadest possible RBAC grant.",
                        remediation="Replace with the narrowest Role/ClusterRole that covers what this "
                        "subject actually needs.",
                    )
                )
        for binding in inventory.role_bindings:
            if binding.role_ref.kind != "ClusterRole" or binding.role_ref.name != _CLUSTER_ADMIN_ROLE_NAME:
                continue
            for subject in binding.subjects or []:
                if self._is_builtin_subject(subject):
                    continue
                findings.append(
                    SecurityFinding(
                        finding_id=str(uuid4()),
                        rule_id=self.rule_id,
                        severity=SecuritySeverity.HIGH,
                        resource=SecurityResourceRef(
                            namespace=binding.metadata.namespace, kind="RoleBinding", name=binding.metadata.name
                        ),
                        evidence=[
                            f"RoleBinding '{binding.metadata.name}' in '{binding.metadata.namespace}' grants "
                            f"'cluster-admin' (scoped to this namespace) to {subject.kind} '{subject.name}'"
                        ],
                        explanation="cluster-admin is a cluster-scoped ClusterRole -- binding it via a "
                        "namespaced RoleBinding still grants every verb on every resource within this "
                        "one namespace, far broader than almost any workload genuinely needs.",
                        remediation="Replace with the narrowest Role that covers what this subject "
                        "actually needs in this namespace.",
                    )
                )
        return findings


class WildcardRbacRuleRule(SecurityRule):
    rule_id = "WILDCARD_RBAC_RULE"

    def __init__(self, settings: Settings):
        self._builtin_names = set(settings.security_builtin_wildcard_role_name_list)

    def _is_builtin_role(self, name: str) -> bool:
        return name.startswith("system:") or name in self._builtin_names

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for cluster_role in inventory.cluster_roles:
            if self._is_builtin_role(cluster_role.metadata.name):
                continue
            findings.extend(self._findings_for_role(cluster_role, namespace=None, kind="ClusterRole"))
        for role in inventory.roles:
            if self._is_builtin_role(role.metadata.name):
                continue
            findings.extend(self._findings_for_role(role, namespace=role.metadata.namespace, kind="Role"))
        return findings

    def _findings_for_role(self, role, namespace: str | None, kind: str) -> list[SecurityFinding]:
        findings = []
        for rule in role.rules or []:
            verbs = rule.verbs or []
            resources = rule.resources or []
            api_groups = rule.api_groups or []
            if "*" in verbs and ("*" in resources or "*" in api_groups):
                findings.append(
                    SecurityFinding(
                        finding_id=str(uuid4()),
                        rule_id=self.rule_id,
                        severity=SecuritySeverity.HIGH,
                        resource=SecurityResourceRef(namespace=namespace, kind=kind, name=role.metadata.name),
                        evidence=[f"rule: apiGroups={api_groups}, resources={resources}, verbs={verbs}"],
                        explanation=f"This {kind} has a rule combining a wildcard verb with a wildcard "
                        "resource or apiGroup -- effectively unrestricted within its scope.",
                        remediation="Enumerate the specific apiGroups/resources/verbs this subject actually "
                        "needs instead of using '*'.",
                    )
                )
        return findings

"""Resolves every RoleBinding/ClusterRoleBinding's `roleRef` to the real
Role/ClusterRole object it names, and groups the result by subject --
the "effective permissions, combined across bindings" gap named in
docs/SECURITY_AUDITING.md Section 12/13. Pure functions over an
already-built `SecurityInventory`, no new API calls: every object this
needs is already fetched by `build_security_inventory()`.
"""

from __future__ import annotations

from dataclasses import dataclass

from kubernetes.client import V1ClusterRole, V1PolicyRule, V1Role

from kubesentinel.security.inventory import SecurityInventory

_RBAC_GROUP = "rbac.authorization.k8s.io"


@dataclass(frozen=True)
class SubjectKey:
    kind: str
    name: str
    namespace: str | None = None


@dataclass(frozen=True)
class ResolvedBinding:
    """One binding naming a subject, with the rules its `roleRef` actually
    resolved to -- `None` rules means the Role/ClusterRole it names
    wasn't found in the inventory (never guessed at)."""

    binding_kind: str  # "RoleBinding" | "ClusterRoleBinding"
    binding_name: str
    binding_namespace: str | None
    role_kind: str
    role_name: str
    rules: list[V1PolicyRule]


def _roles_by_namespace_and_name(roles: list[V1Role]) -> dict[tuple[str, str], V1Role]:
    return {(r.metadata.namespace, r.metadata.name): r for r in roles}


def _cluster_roles_by_name(cluster_roles: list[V1ClusterRole]) -> dict[str, V1ClusterRole]:
    return {r.metadata.name: r for r in cluster_roles}


def resolve_subject_bindings(inventory: SecurityInventory) -> dict[SubjectKey, list[ResolvedBinding]]:
    """For every binding in `inventory`, resolves its `roleRef` and
    groups the result by every subject the binding names."""
    roles_by_key = _roles_by_namespace_and_name(inventory.roles)
    cluster_roles_by_name = _cluster_roles_by_name(inventory.cluster_roles)

    result: dict[SubjectKey, list[ResolvedBinding]] = {}

    def _add(subject_key: SubjectKey, resolved: ResolvedBinding) -> None:
        result.setdefault(subject_key, []).append(resolved)

    for binding in inventory.cluster_role_bindings:
        role_ref = binding.role_ref
        if role_ref.kind != "ClusterRole":
            continue
        cluster_role = cluster_roles_by_name.get(role_ref.name)
        if cluster_role is None:
            continue
        resolved = ResolvedBinding(
            binding_kind="ClusterRoleBinding",
            binding_name=binding.metadata.name,
            binding_namespace=None,
            role_kind="ClusterRole",
            role_name=role_ref.name,
            rules=cluster_role.rules or [],
        )
        for subject in binding.subjects or []:
            _add(SubjectKey(kind=subject.kind, name=subject.name, namespace=subject.namespace), resolved)

    for binding in inventory.role_bindings:
        role_ref = binding.role_ref
        if role_ref.kind == "Role":
            role = roles_by_key.get((binding.metadata.namespace, role_ref.name))
            if role is None:
                continue
            rules = role.rules or []
        elif role_ref.kind == "ClusterRole":
            cluster_role = cluster_roles_by_name.get(role_ref.name)
            if cluster_role is None:
                continue
            rules = cluster_role.rules or []
        else:
            continue
        resolved = ResolvedBinding(
            binding_kind="RoleBinding",
            binding_name=binding.metadata.name,
            binding_namespace=binding.metadata.namespace,
            role_kind=role_ref.kind,
            role_name=role_ref.name,
            rules=rules,
        )
        for subject in binding.subjects or []:
            _add(SubjectKey(kind=subject.kind, name=subject.name, namespace=subject.namespace), resolved)

    return result


def has_verb_on_resource(rule: V1PolicyRule, verb: str, api_group: str, resource: str) -> bool:
    """True only for a literal match on all three -- deliberately never
    treats a wildcard verb/resource/apiGroup as satisfying a specific
    one here. That is `WILDCARD_RBAC_RULE`'s own, already-existing job;
    conflating the two would double-count the same underlying fact under
    two different rule_ids."""
    verbs = rule.verbs or []
    resources = rule.resources or []
    api_groups = rule.api_groups or []
    return verb in verbs and resource in resources and api_group in api_groups


def bindings_granting_verb_on_rbac_objects(
    bindings: list[ResolvedBinding], verb: str
) -> list[tuple[ResolvedBinding, str]]:
    """Every binding (among `bindings`, which the caller has already
    filtered to one subject) whose resolved rules grant `verb` on
    `roles` or `clusterroles` in the `rbac.authorization.k8s.io` group --
    paired with which of the two resources matched, for evidence text."""
    matches = []
    for binding in bindings:
        for rule in binding.rules:
            for resource in ("roles", "clusterroles"):
                if has_verb_on_resource(rule, verb, _RBAC_GROUP, resource):
                    matches.append((binding, resource))
                    break
    return matches

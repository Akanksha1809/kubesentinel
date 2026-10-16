"""Builds the cluster state the security auditor's rules need: one Pod
spec per owning workload (deduped the same way
`optimization/inventory.py::build_workload_inventory` already groups
Pods, reusing `optimization/ownership.py::resolve_pod_owner` rather than
re-deriving owner resolution a fourth time), RBAC objects, NetworkPolicy
objects, and Deployments/StatefulSets/PodDisruptionBudgets.

One-shot, no watch loop -- the same shape every other engine in this
project uses. A sub-read that fails (e.g. 403 listing ClusterRoleBindings)
is recorded in `warnings` and that one rule family evaluates against an
empty list, rather than aborting the whole audit -- see
docs/SECURITY_AUDITING.md Section 5/9.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kubernetes.client import (
    V1ClusterRole,
    V1ClusterRoleBinding,
    V1Deployment,
    V1NetworkPolicy,
    V1PodDisruptionBudget,
    V1PodSpec,
    V1Role,
    V1RoleBinding,
    V1StatefulSet,
)
from kubernetes.client.rest import ApiException

from kubesentinel.observer.client import ClusterClients
from kubesentinel.optimization.inventory import list_all
from kubesentinel.optimization.ownership import resolve_pod_owner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkloadPodTemplate:
    """One owning workload's representative Pod spec -- every container-
    security rule evaluates this once per workload, never once per
    replica (replicas of the same workload share the same template)."""

    namespace: str
    workload_kind: str
    workload_name: str
    pod_spec: V1PodSpec


@dataclass
class SecurityInventory:
    pod_templates: list[WorkloadPodTemplate] = field(default_factory=list)
    cluster_role_bindings: list[V1ClusterRoleBinding] = field(default_factory=list)
    cluster_roles: list[V1ClusterRole] = field(default_factory=list)
    role_bindings: list[V1RoleBinding] = field(default_factory=list)
    roles: list[V1Role] = field(default_factory=list)
    network_policies_by_namespace: dict[str, list[V1NetworkPolicy]] = field(default_factory=dict)
    namespaces_with_pods: set[str] = field(default_factory=set)
    deployments: list[V1Deployment] = field(default_factory=list)
    stateful_sets: list[V1StatefulSet] = field(default_factory=list)
    pod_disruption_budgets: list[V1PodDisruptionBudget] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_security_inventory(clients: ClusterClients, namespaces: list[str] | None = None) -> SecurityInventory:
    namespaces = namespaces or []
    inventory = SecurityInventory()

    def _safe_list_all(label: str, list_namespaced, list_all_namespaces) -> list:
        try:
            return list_all(list_namespaced, list_all_namespaces, namespaces)
        except ApiException as exc:
            inventory.warnings.append(f"could not list {label}: {exc.reason} (status={exc.status})")
            return []

    pods = _safe_list_all("Pods", clients.core.list_namespaced_pod, clients.core.list_pod_for_all_namespaces)
    replica_sets = _safe_list_all(
        "ReplicaSets", clients.apps.list_namespaced_replica_set, clients.apps.list_replica_set_for_all_namespaces
    )

    replicasets_by_uid = {rs.metadata.uid: rs for rs in replica_sets}
    seen_workloads: set[tuple[str, str, str]] = set()
    for pod in pods:
        if pod.metadata.deletion_timestamp is not None:
            continue
        inventory.namespaces_with_pods.add(pod.metadata.namespace)
        owner = resolve_pod_owner(pod, replicasets_by_uid)
        if owner is None:
            continue
        key = (pod.metadata.namespace, owner.kind.value, owner.name)
        if key in seen_workloads:
            continue
        seen_workloads.add(key)
        inventory.pod_templates.append(
            WorkloadPodTemplate(
                namespace=pod.metadata.namespace,
                workload_kind=owner.kind.value,
                workload_name=owner.name,
                pod_spec=pod.spec,
            )
        )

    if namespaces:
        for namespace in namespaces:
            try:
                inventory.role_bindings.extend(
                    clients.rbac_authorization.list_namespaced_role_binding(namespace=namespace).items
                )
                inventory.roles.extend(clients.rbac_authorization.list_namespaced_role(namespace=namespace).items)
            except ApiException as exc:
                inventory.warnings.append(
                    f"could not list Roles/RoleBindings in {namespace}: {exc.reason} (status={exc.status})"
                )
    else:
        try:
            inventory.role_bindings = clients.rbac_authorization.list_role_binding_for_all_namespaces().items
            inventory.roles = clients.rbac_authorization.list_role_for_all_namespaces().items
        except ApiException as exc:
            inventory.warnings.append(f"could not list Roles/RoleBindings: {exc.reason} (status={exc.status})")

    try:
        inventory.cluster_role_bindings = clients.rbac_authorization.list_cluster_role_binding().items
    except ApiException as exc:
        inventory.warnings.append(f"could not list ClusterRoleBindings: {exc.reason} (status={exc.status})")
    try:
        inventory.cluster_roles = clients.rbac_authorization.list_cluster_role().items
    except ApiException as exc:
        inventory.warnings.append(f"could not list ClusterRoles: {exc.reason} (status={exc.status})")

    target_namespaces = namespaces or sorted(inventory.namespaces_with_pods)
    for namespace in target_namespaces:
        try:
            policies = clients.networking.list_namespaced_network_policy(namespace=namespace).items
        except ApiException as exc:
            inventory.warnings.append(f"could not list NetworkPolicies in {namespace}: {exc.reason} (status={exc.status})")
            policies = []
        inventory.network_policies_by_namespace[namespace] = policies

    inventory.deployments = _safe_list_all(
        "Deployments", clients.apps.list_namespaced_deployment, clients.apps.list_deployment_for_all_namespaces
    )
    inventory.stateful_sets = _safe_list_all(
        "StatefulSets", clients.apps.list_namespaced_stateful_set, clients.apps.list_stateful_set_for_all_namespaces
    )

    if namespaces:
        for namespace in namespaces:
            try:
                inventory.pod_disruption_budgets.extend(
                    clients.policy.list_namespaced_pod_disruption_budget(namespace=namespace).items
                )
            except ApiException as exc:
                inventory.warnings.append(
                    f"could not list PodDisruptionBudgets in {namespace}: {exc.reason} (status={exc.status})"
                )
    else:
        try:
            inventory.pod_disruption_budgets = clients.policy.list_pod_disruption_budget_for_all_namespaces().items
        except ApiException as exc:
            inventory.warnings.append(f"could not list PodDisruptionBudgets: {exc.reason} (status={exc.status})")

    return inventory

"""Orchestrates the per-node constraint checks into one `SchedulingFinding`
for a target Pod -- a real, already-Pending Pod, or a hypothetical spec a
caller wants simulated before creating it.

A hypothetical spec (a raw dict, e.g. from an API request body) is
deserialized into a real, typed `V1PodSpec` via the Kubernetes client's own
deserializer before analysis -- so `constraints.py`'s functions, which read
real `V1PodSpec`/`V1Toleration`/etc. attributes, work identically on a real
Pod's spec and a hypothetical one, with no second code path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from kubernetes import client as k8s_client

from kubesentinel.models.optimization import ResourceQuantity
from kubesentinel.models.scheduling import (
    EVALUATED_CONSTRAINT_TYPES,
    ConstraintType,
    NodeFitResult,
    NodeSnapshot,
    SchedulingFinding,
)
from kubesentinel.scheduling.constraints import (
    check_namespace_quota,
    check_node_selector,
    check_required_node_affinity,
    check_resource_fit,
    check_taints_tolerations,
)
from kubesentinel.scheduling.node_inventory import parse_quantity_dict


def deserialize_pod_spec(raw_spec: dict) -> k8s_client.V1PodSpec:
    """Converts a raw (e.g. JSON request body) Pod spec dict into a real,
    typed `V1PodSpec` -- the same deserializer the generated client itself
    uses on a real API response, so a hypothetical spec is read by exactly
    the same attribute-based code a real Pod's spec is."""
    return k8s_client.ApiClient()._ApiClient__deserialize(raw_spec, "V1PodSpec")  # noqa: SLF001


def _pod_spec_request(pod_spec: k8s_client.V1PodSpec) -> ResourceQuantity:
    cpu_total = 0
    memory_total = 0
    any_cpu = False
    any_memory = False
    for container in pod_spec.containers or []:
        resources = container.resources
        if resources is None or resources.requests is None:
            continue
        parsed = parse_quantity_dict(resources.requests, context=f"hypothetical/{container.name}")
        if parsed.cpu_millicores is not None:
            cpu_total += parsed.cpu_millicores
            any_cpu = True
        if parsed.memory_bytes is not None:
            memory_total += parsed.memory_bytes
            any_memory = True
    return ResourceQuantity(
        cpu_millicores=cpu_total if any_cpu else None,
        memory_bytes=memory_total if any_memory else None,
    )


def _detect_unevaluated_constraints(pod_spec: k8s_client.V1PodSpec) -> list[ConstraintType]:
    found: list[ConstraintType] = []
    affinity = pod_spec.affinity
    if affinity and affinity.node_affinity and affinity.node_affinity.preferred_during_scheduling_ignored_during_execution:
        found.append(ConstraintType.PREFERRED_NODE_AFFINITY)
    if affinity and affinity.pod_affinity:
        found.append(ConstraintType.POD_AFFINITY)
    if affinity and affinity.pod_anti_affinity:
        found.append(ConstraintType.POD_ANTI_AFFINITY)
    if pod_spec.topology_spread_constraints:
        found.append(ConstraintType.TOPOLOGY_SPREAD)
    return found


def analyze_pod_scheduling(
    pod_spec: k8s_client.V1PodSpec,
    nodes: list[NodeSnapshot],
    *,
    namespace: str,
    quotas: list | None = None,
    check_quota: bool = False,
    pod_name: str | None = None,
    workload_kind: str | None = None,
    workload_name: str | None = None,
    scheduler_message: str | None = None,
    now: datetime | None = None,
) -> SchedulingFinding:
    """`check_quota` is deliberately a separate flag from "quotas is
    non-empty": a ResourceQuota is an *admission-time* check, not a
    scheduling-time one -- a real, already-created Pod already passed it
    (or it wouldn't exist), so checking it again doesn't explain why that
    Pod is Pending. It's only meaningful for a *hypothetical* spec, where
    it answers "would creating this Pod even be admitted" ahead of
    "would it then be schedulable." See docs/SCHEDULING_ANALYSIS.md."""
    now = now or datetime.now(timezone.utc)
    pod_request = _pod_spec_request(pod_spec)
    tolerations = pod_spec.tolerations or []
    node_selector = pod_spec.node_selector
    affinity = pod_spec.affinity

    evaluated = sorted(EVALUATED_CONSTRAINT_TYPES - {ConstraintType.NAMESPACE_QUOTA}, key=lambda c: c.value)
    quota_result = None
    if check_quota:
        quota_result = check_namespace_quota(pod_request, quotas or [])
        evaluated = [ConstraintType.NAMESPACE_QUOTA, *evaluated]

    node_results: list[NodeFitResult] = []
    for node in nodes:
        if node.unschedulable or not node.ready:
            node_results.append(
                NodeFitResult(
                    node_name=node.name,
                    schedulable=False,
                    constraint_results=[],
                )
            )
            continue
        results = [
            check_resource_fit(pod_request, node),
            check_taints_tolerations(tolerations, node),
            check_node_selector(node_selector, node),
            check_required_node_affinity(affinity, node),
        ]
        node_results.append(
            NodeFitResult(
                node_name=node.name,
                schedulable=all(r.satisfied for r in results),
                constraint_results=results,
            )
        )

    any_node_schedulable = any(r.schedulable for r in node_results)
    if check_quota and quota_result is not None and not quota_result.satisfied:
        any_node_schedulable = False

    unevaluated = _detect_unevaluated_constraints(pod_spec)

    reasoning_parts = []
    if check_quota and quota_result is not None:
        reasoning_parts.append(f"Namespace quota: {quota_result.reasoning}.")
    if any_node_schedulable:
        schedulable_nodes = [r.node_name for r in node_results if r.schedulable]
        reasoning_parts.append(f"Schedulable on: {', '.join(schedulable_nodes)}.")
    else:
        reasoning_parts.append(
            f"Not schedulable on any of {len(node_results)} node(s) -- see per-node results for the specific "
            "blocking constraint(s)."
        )
    if unevaluated:
        reasoning_parts.append(
            f"Also present but not evaluated by this engine: {', '.join(c.value for c in unevaluated)} -- "
            "see docs/SCHEDULING_ANALYSIS.md for why."
        )

    return SchedulingFinding(
        finding_id=str(uuid.uuid4()),
        namespace=namespace,
        pod_name=pod_name,
        workload_kind=workload_kind,
        workload_name=workload_name,
        analyzed_at=now,
        scheduler_message=scheduler_message,
        node_results=node_results,
        evaluated_constraints=evaluated,
        unevaluated_constraints=unevaluated,
        any_node_schedulable=any_node_schedulable,
        reasoning=" ".join(reasoning_parts),
    )

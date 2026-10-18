"""Builds the Node-side state the scheduling advisor needs: allocatable
capacity, already-requested resources (the scheduler's own accounting
unit -- the sum of every bound Pod's own requests, never live usage),
already-specified limits, taints, and labels.

Read-only: only ever `list_node`/`list_pod_for_all_namespaces`. No watch
loop -- like `optimization/inventory.py`, this takes a one-shot snapshot
per analysis call, not a continuously-maintained cache.

`limits`/`unlimited_*_container_count` were added for
`optimization/overcommitment.py`'s cluster-level overcommitment analysis
(RESOURCE_OPTIMIZATION_DESIGN.md's own named "cluster-level overcommitment
analysis" gap) -- the scheduling advisor itself has no use for limits
(the real scheduler's own admission math only ever looks at requests), so
this is additive data riding along on the same per-Node, per-Pod loop
`requested` already computes, not a second Node/Pod listing.
"""

from __future__ import annotations

import logging

from kubesentinel.models.optimization import ResourceQuantity
from kubesentinel.models.scheduling import NodeSnapshot, NodeTaint, PodLabelSummary
from kubesentinel.observer.client import ClusterClients
from kubesentinel.optimization.quantities import (
    QuantityParseError,
    parse_cpu_to_millicores,
    parse_memory_to_bytes,
)

logger = logging.getLogger(__name__)

#: Pods in these phases no longer hold resources on their Node -- the real
#: scheduler's own accounting excludes them from "already requested" the
#: same way. A Pending Pod with `node_name` unset was never counted in the
#: first place (it isn't bound to any Node yet).
_TERMINAL_PHASES = {"Succeeded", "Failed"}


def parse_quantity_dict(raw: dict, *, context: str) -> ResourceQuantity:
    cpu_millicores = None
    memory_bytes = None
    if raw.get("cpu"):
        try:
            cpu_millicores = parse_cpu_to_millicores(raw["cpu"])
        except QuantityParseError:
            logger.warning("unparseable cpu quantity %r in %s", raw["cpu"], context)
    if raw.get("memory"):
        try:
            memory_bytes = parse_memory_to_bytes(raw["memory"])
        except QuantityParseError:
            logger.warning("unparseable memory quantity %r in %s", raw["memory"], context)
    return ResourceQuantity(cpu_millicores=cpu_millicores, memory_bytes=memory_bytes)


def _pod_requested(pod) -> ResourceQuantity:
    """Sums every container's own `requests` -- init containers are
    deliberately not included (the real scheduler takes the *max* of
    init-container and regular-container requests per resource, not a
    sum; approximated here as regular-container-only, a documented
    simplification, not an oversight)."""
    cpu_total = 0
    memory_total = 0
    any_cpu = False
    any_memory = False
    for container in pod.spec.containers or []:
        resources = container.resources
        if resources is None or resources.requests is None:
            continue
        parsed = parse_quantity_dict(resources.requests, context=f"{pod.metadata.name}/{container.name}")
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


def _pod_limits(pod) -> tuple[ResourceQuantity, int, int]:
    """Sums every (regular, non-init) container's own `limits` that
    actually specifies one, same init-container simplification as
    `_pod_requested`. Also returns how many of this Pod's containers
    specify no cpu/no memory limit at all -- a missing limit means
    unbounded, real worst-case consumption, never "contributes 0" to the
    sum, so callers must track it separately rather than let it
    disappear into the total."""
    cpu_total = 0
    memory_total = 0
    any_cpu = False
    any_memory = False
    unlimited_cpu = 0
    unlimited_memory = 0
    for container in pod.spec.containers or []:
        resources = container.resources
        limits = resources.limits if resources is not None else None
        parsed = (
            parse_quantity_dict(limits, context=f"{pod.metadata.name}/{container.name}") if limits else ResourceQuantity()
        )
        if parsed.cpu_millicores is not None:
            cpu_total += parsed.cpu_millicores
            any_cpu = True
        else:
            unlimited_cpu += 1
        if parsed.memory_bytes is not None:
            memory_total += parsed.memory_bytes
            any_memory = True
        else:
            unlimited_memory += 1
    return (
        ResourceQuantity(
            cpu_millicores=cpu_total if any_cpu else None,
            memory_bytes=memory_total if any_memory else None,
        ),
        unlimited_cpu,
        unlimited_memory,
    )


def _node_ready(node) -> bool:
    for condition in node.status.conditions or []:
        if condition.type == "Ready":
            return condition.status == "True"
    return False


def build_node_inventory(clients: ClusterClients) -> list[NodeSnapshot]:
    nodes = clients.core.list_node().items
    all_pods = clients.core.list_pod_for_all_namespaces().items

    requested_by_node: dict[str, list] = {n.metadata.name: [] for n in nodes}
    for pod in all_pods:
        node_name = pod.spec.node_name
        if not node_name or node_name not in requested_by_node:
            continue
        if pod.status and pod.status.phase in _TERMINAL_PHASES:
            continue
        requested_by_node[node_name].append(pod)

    snapshots: list[NodeSnapshot] = []
    for node in nodes:
        allocatable_raw = node.status.allocatable or {}
        allocatable = parse_quantity_dict(allocatable_raw, context=f"node/{node.metadata.name} allocatable")

        cpu_requested = 0
        memory_requested = 0
        cpu_limited = 0
        memory_limited = 0
        unlimited_cpu_containers = 0
        unlimited_memory_containers = 0
        for pod in requested_by_node[node.metadata.name]:
            pod_req = _pod_requested(pod)
            cpu_requested += pod_req.cpu_millicores or 0
            memory_requested += pod_req.memory_bytes or 0
            pod_limits, pod_unlimited_cpu, pod_unlimited_memory = _pod_limits(pod)
            cpu_limited += pod_limits.cpu_millicores or 0
            memory_limited += pod_limits.memory_bytes or 0
            unlimited_cpu_containers += pod_unlimited_cpu
            unlimited_memory_containers += pod_unlimited_memory

        taints = [
            NodeTaint(key=t.key, value=t.value, effect=t.effect) for t in (node.spec.taints or [])
        ]

        snapshots.append(
            NodeSnapshot(
                name=node.metadata.name,
                allocatable=allocatable,
                requested=ResourceQuantity(cpu_millicores=cpu_requested, memory_bytes=memory_requested),
                limits=ResourceQuantity(cpu_millicores=cpu_limited, memory_bytes=memory_limited),
                unlimited_cpu_container_count=unlimited_cpu_containers,
                unlimited_memory_container_count=unlimited_memory_containers,
                unschedulable=bool(node.spec.unschedulable),
                taints=taints,
                labels=dict(node.metadata.labels or {}),
                ready=_node_ready(node),
            )
        )
    return snapshots


def build_pod_label_inventory(clients: ClusterClients) -> list[PodLabelSummary]:
    """Every already-scheduled Pod's (namespace, node, labels) --
    everything `check_pod_affinity`/`check_pod_anti_affinity`/
    `check_topology_spread_constraints` need to know about Pods *other*
    than the one being analyzed. A second, separate
    `list_pod_for_all_namespaces()` call rather than folding this into
    `build_node_inventory` above: that function's own callers
    (`optimization/service.py` among them) have no use for pod-label
    data, and changing its return shape to carry this too would force
    every caller to receive and discard it. One extra full-namespace Pod
    list per scheduling analysis is a negligible cost for a local,
    lab-scale cluster, not worth the more invasive alternative.
    """
    pods = clients.core.list_pod_for_all_namespaces().items
    summaries: list[PodLabelSummary] = []
    for pod in pods:
        if not pod.spec.node_name:
            continue
        if pod.status and pod.status.phase in _TERMINAL_PHASES:
            continue
        summaries.append(
            PodLabelSummary(
                uid=pod.metadata.uid,
                namespace=pod.metadata.namespace,
                node_name=pod.spec.node_name,
                labels=dict(pod.metadata.labels or {}),
            )
        )
    return summaries

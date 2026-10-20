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

`effective_pod_resource_quantity()` closes this module's own previously-
named approximation ("resource-fit math sums only regular containers'
requests, not init containers"): the real scheduler's effective-request
algorithm for one Pod, now shared by this module's own per-Pod requests/
limits accounting and by `scheduling/analyzer.py::_pod_spec_request`'s
own candidate-Pod math, so the two never drift to different answers for
the same question.
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


def _container_resource_quantity(container, field: str, *, context: str) -> ResourceQuantity:
    resources = container.resources
    values = getattr(resources, field, None) if resources is not None else None
    return parse_quantity_dict(values, context=context) if values else ResourceQuantity()


def effective_pod_resource_quantity(
    containers: list | None, init_containers: list | None, field: str, *, context: str
) -> ResourceQuantity:
    """Kubernetes' own effective-Pod-resource algorithm for `field`
    (`"requests"` or `"limits"`, read identically for both -- the same
    formula governs both). See
    https://kubernetes.io/docs/concepts/workloads/pods/init-containers/#resources
    and, for the sidecar extension below,
    https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/#resource-sharing-within-containers.

    Init containers run one at a time, in order, never concurrently with
    each other -- so a plain (non-restarting) init container only ever
    competes against the single largest one, never a sum across them.
    `restartPolicy: Always` init containers ("native sidecars,"
    Kubernetes 1.28+) are the one exception: once started, a sidecar
    keeps running in the background for the rest of the Pod's life,
    concurrently with every later init container *and* every regular
    container -- closed here, not just competing in the init-container
    max the way a plain init container does, by walking the init
    container list in declared order and tracking a running sidecar
    total: at the moment any given init container is the one actively
    initializing, what's really in use is every earlier sidecar still
    running (the running total) plus that container's own request
    (whichever kind it is) -- and once every init container has run,
    the final steady state is every regular container plus every
    sidecar, all running together. The effective value is the greater
    of every one of those moments, exactly matching the Kubernetes docs'
    own worked example."""
    cpu_sum = 0
    memory_sum = 0
    any_cpu_sum = False
    any_memory_sum = False
    for container in containers or []:
        parsed = _container_resource_quantity(container, field, context=f"{context}/{container.name}")
        if parsed.cpu_millicores is not None:
            cpu_sum += parsed.cpu_millicores
            any_cpu_sum = True
        if parsed.memory_bytes is not None:
            memory_sum += parsed.memory_bytes
            any_memory_sum = True

    cpu_sidecar_sum = 0
    memory_sidecar_sum = 0
    any_cpu_sidecar_sum = False
    any_memory_sidecar_sum = False
    cpu_init_max: int | None = None
    memory_init_max: int | None = None
    for container in init_containers or []:
        parsed = _container_resource_quantity(container, field, context=f"{context}/{container.name} (init)")
        if parsed.cpu_millicores is not None:
            candidate = cpu_sidecar_sum + parsed.cpu_millicores
            cpu_init_max = candidate if cpu_init_max is None else max(cpu_init_max, candidate)
        if parsed.memory_bytes is not None:
            candidate = memory_sidecar_sum + parsed.memory_bytes
            memory_init_max = candidate if memory_init_max is None else max(memory_init_max, candidate)

        if container.restart_policy == "Always":
            if parsed.cpu_millicores is not None:
                cpu_sidecar_sum += parsed.cpu_millicores
                any_cpu_sidecar_sum = True
            if parsed.memory_bytes is not None:
                memory_sidecar_sum += parsed.memory_bytes
                any_memory_sidecar_sum = True

    any_cpu_steady = any_cpu_sum or any_cpu_sidecar_sum
    any_memory_steady = any_memory_sum or any_memory_sidecar_sum
    cpu_steady = cpu_sum + cpu_sidecar_sum
    memory_steady = memory_sum + memory_sidecar_sum

    cpu_candidates = [v for v in (cpu_steady if any_cpu_steady else None, cpu_init_max) if v is not None]
    memory_candidates = [v for v in (memory_steady if any_memory_steady else None, memory_init_max) if v is not None]
    return ResourceQuantity(
        cpu_millicores=max(cpu_candidates) if cpu_candidates else None,
        memory_bytes=max(memory_candidates) if memory_candidates else None,
    )


def _pod_requested(pod) -> ResourceQuantity:
    return effective_pod_resource_quantity(
        pod.spec.containers, pod.spec.init_containers, "requests", context=pod.metadata.name
    )


def _pod_limits(pod) -> tuple[ResourceQuantity, int, int]:
    """Effective Pod limits via the same real-scheduler algorithm
    `effective_pod_resource_quantity` implements for requests. Also
    returns how many of this Pod's containers (regular *and* init) carry
    no cpu/no memory limit at all -- a missing limit means unbounded,
    real worst-case consumption, never "contributes 0" to the total, so
    callers must track it separately rather than let it disappear."""
    effective = effective_pod_resource_quantity(
        pod.spec.containers, pod.spec.init_containers, "limits", context=pod.metadata.name
    )
    unlimited_cpu = 0
    unlimited_memory = 0
    for container in [*(pod.spec.containers or []), *(pod.spec.init_containers or [])]:
        parsed = _container_resource_quantity(container, "limits", context=f"{pod.metadata.name}/{container.name}")
        if parsed.cpu_millicores is None:
            unlimited_cpu += 1
        if parsed.memory_bytes is None:
            unlimited_memory += 1
    return effective, unlimited_cpu, unlimited_memory


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

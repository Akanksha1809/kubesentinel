"""Cluster-level resource overcommitment analysis
(RESOURCE_OPTIMIZATION_DESIGN.md's own named "cluster-level overcommitment
analysis" gap, deferred at the engine's original scope because it needs
Node allocatable capacity aggregated across every workload -- a shape that
doesn't fit the per-container `ResourceRecommendation` model at all).

A pure function over `NodeSnapshot`s `scheduling/node_inventory.py` already
builds (and `OptimizationService.analyze()` already fetches, for its own
capacity-headroom check) -- no new cluster read, no new RBAC, same
"orchestration lives in service.py, analysis stays pure" split every
module in this engine already follows.

Two distinct ratios, deliberately not collapsed into one number:

- *requested*-vs-allocatable should never exceed 1.0 -- the real scheduler
  refuses to bind a Pod past a Node's allocatable capacity. A ratio above
  1.0 here is a genuine anomaly (a static/mirror Pod bypassing normal
  admission, or a race), not a tuning signal.
- *limits*-vs-allocatable is the actionable overcommitment signal.
  Kubernetes does not enforce this at admission at all, so it is entirely
  normal for it to exceed 1.0 -- it only becomes a real risk ("every
  container hitting its own limit at once would exceed this Node's real
  capacity") past a configurable threshold.

A container with no limit set at all is never treated as contributing 0 --
`NodeSnapshot.unlimited_*_container_count` is carried through so a
caller can never mistake "no limit specified" for "confirmed safe."
"""

from __future__ import annotations

from datetime import datetime, timezone

from kubesentinel.config import Settings
from kubesentinel.models.optimization import ClusterOvercommitmentSummary, NodeOvercommitment, ResourceQuantity
from kubesentinel.models.scheduling import NodeSnapshot


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    """None whenever either side is unknown or the denominator is zero/
    negative -- never a ratio computed against a missing or meaningless
    allocatable figure."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _node_overcommitment(node: NodeSnapshot, settings: Settings) -> NodeOvercommitment:
    threshold = settings.optimization_overcommitment_limits_ratio_threshold
    limits_cpu_ratio = _ratio(node.limits.cpu_millicores, node.allocatable.cpu_millicores)
    limits_memory_ratio = _ratio(node.limits.memory_bytes, node.allocatable.memory_bytes)
    return NodeOvercommitment(
        node_name=node.name,
        allocatable=node.allocatable,
        requested=node.requested,
        limits=node.limits,
        requested_cpu_ratio=_ratio(node.requested.cpu_millicores, node.allocatable.cpu_millicores),
        requested_memory_ratio=_ratio(node.requested.memory_bytes, node.allocatable.memory_bytes),
        limits_cpu_ratio=limits_cpu_ratio,
        limits_memory_ratio=limits_memory_ratio,
        unlimited_cpu_container_count=node.unlimited_cpu_container_count,
        unlimited_memory_container_count=node.unlimited_memory_container_count,
        overcommitted_cpu=limits_cpu_ratio is not None and limits_cpu_ratio > threshold,
        overcommitted_memory=limits_memory_ratio is not None and limits_memory_ratio > threshold,
    )


def compute_cluster_overcommitment(
    node_snapshots: list[NodeSnapshot], settings: Settings, *, now: datetime | None = None
) -> ClusterOvercommitmentSummary | None:
    """Returns `None` only when there are no Nodes to report on at all
    (e.g. the Node inventory read itself failed this pass) -- never a
    zero-filled summary, which would misread as "confirmed not
    overcommitted." A Node that is unschedulable/not-Ready is still
    included: its own already-bound workloads still really consume (or
    could still spike to) its capacity, cordoning doesn't change that.
    """
    if not node_snapshots:
        return None

    nodes = [_node_overcommitment(node, settings) for node in node_snapshots]

    def _sum(attr: str, quantity_attr: str) -> int | None:
        values = [getattr(getattr(node, quantity_attr), attr) for node in node_snapshots]
        if any(v is None for v in values):
            return None
        return sum(values)

    cluster_allocatable = ResourceQuantity(
        cpu_millicores=_sum("cpu_millicores", "allocatable"), memory_bytes=_sum("memory_bytes", "allocatable")
    )
    cluster_requested = ResourceQuantity(
        cpu_millicores=_sum("cpu_millicores", "requested"), memory_bytes=_sum("memory_bytes", "requested")
    )
    cluster_limits = ResourceQuantity(
        cpu_millicores=_sum("cpu_millicores", "limits"), memory_bytes=_sum("memory_bytes", "limits")
    )

    threshold = settings.optimization_overcommitment_limits_ratio_threshold
    cluster_limits_cpu_ratio = _ratio(cluster_limits.cpu_millicores, cluster_allocatable.cpu_millicores)
    cluster_limits_memory_ratio = _ratio(cluster_limits.memory_bytes, cluster_allocatable.memory_bytes)

    return ClusterOvercommitmentSummary(
        computed_at=now or datetime.now(timezone.utc),
        nodes=nodes,
        cluster_requested_cpu_ratio=_ratio(cluster_requested.cpu_millicores, cluster_allocatable.cpu_millicores),
        cluster_requested_memory_ratio=_ratio(cluster_requested.memory_bytes, cluster_allocatable.memory_bytes),
        cluster_limits_cpu_ratio=cluster_limits_cpu_ratio,
        cluster_limits_memory_ratio=cluster_limits_memory_ratio,
        overcommitted_cpu=cluster_limits_cpu_ratio is not None and cluster_limits_cpu_ratio > threshold,
        overcommitted_memory=cluster_limits_memory_ratio is not None and cluster_limits_memory_ratio > threshold,
    )

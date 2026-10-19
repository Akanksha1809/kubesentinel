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

Three distinct ratios, deliberately not collapsed into one number:

- *requested*-vs-allocatable should never exceed 1.0 -- the real scheduler
  refuses to bind a Pod past a Node's allocatable capacity. A ratio above
  1.0 here is a genuine anomaly (a static/mirror Pod bypassing normal
  admission, or a race), not a tuning signal.
- *limits*-vs-allocatable is the actionable overcommitment signal.
  Kubernetes does not enforce this at admission at all, so it is entirely
  normal for it to exceed 1.0 -- it only becomes a real risk ("every
  container hitting its own limit at once would exceed this Node's real
  capacity") past a configurable threshold.
- *actual usage*-vs-allocatable (optional -- only when a `MetricsProvider`
  is reachable) is the most actionable of the three: unlike the other
  two, it is measuring fact, not configuration. Closes this engine's own
  previously-named gap ("a Node-metrics ClusterRole... nothing calls
  `MetricsApiProvider.get_current_node_usage()`"): the method already
  existed, it simply had no caller and no RBAC grant for the
  cluster-scoped `metrics.k8s.io/nodes` resource until now (see
  `rbac/optimization-clusterrole.yaml`).

A container with no limit set at all is never treated as contributing 0 --
`NodeSnapshot.unlimited_*_container_count` is carried through so a
caller can never mistake "no limit specified" for "confirmed safe." The
same honest-absence discipline applies to actual usage: a Node this
function has no usage sample for gets `None` ratios, never a `0` that
would misread as "confirmed idle."
"""

from __future__ import annotations

from datetime import datetime, timezone

from kubesentinel.config import Settings
from kubesentinel.models.optimization import ClusterOvercommitmentSummary, NodeOvercommitment, ResourceQuantity
from kubesentinel.models.scheduling import NodeSnapshot
from kubesentinel.optimization.metrics_providers import NodeUsageSample


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    """None whenever either side is unknown or the denominator is zero/
    negative -- never a ratio computed against a missing or meaningless
    allocatable figure."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _node_overcommitment(
    node: NodeSnapshot, settings: Settings, usage: NodeUsageSample | None
) -> NodeOvercommitment:
    threshold = settings.optimization_overcommitment_limits_ratio_threshold
    limits_cpu_ratio = _ratio(node.limits.cpu_millicores, node.allocatable.cpu_millicores)
    limits_memory_ratio = _ratio(node.limits.memory_bytes, node.allocatable.memory_bytes)
    actual_cpu = usage.cpu_millicores if usage is not None else None
    actual_memory = usage.memory_bytes if usage is not None else None
    actual_cpu_ratio = _ratio(actual_cpu, node.allocatable.cpu_millicores)
    actual_memory_ratio = _ratio(actual_memory, node.allocatable.memory_bytes)
    pressure_threshold = settings.optimization_node_pressure_ratio_threshold
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
        actual_cpu_millicores=actual_cpu,
        actual_memory_bytes=actual_memory,
        actual_cpu_ratio=actual_cpu_ratio,
        actual_memory_ratio=actual_memory_ratio,
        under_pressure_cpu=actual_cpu_ratio is not None and actual_cpu_ratio > pressure_threshold,
        under_pressure_memory=actual_memory_ratio is not None and actual_memory_ratio > pressure_threshold,
    )


def compute_cluster_overcommitment(
    node_snapshots: list[NodeSnapshot],
    settings: Settings,
    *,
    node_usage: list[NodeUsageSample] | None = None,
    now: datetime | None = None,
) -> ClusterOvercommitmentSummary | None:
    """Returns `None` only when there are no Nodes to report on at all
    (e.g. the Node inventory read itself failed this pass) -- never a
    zero-filled summary, which would misread as "confirmed not
    overcommitted." A Node that is unschedulable/not-Ready is still
    included: its own already-bound workloads still really consume (or
    could still spike to) its capacity, cordoning doesn't change that.

    `node_usage` is optional and independent of `node_snapshots` --
    omitted entirely (`None`/`[]`) whenever no `MetricsProvider` is
    configured or reachable this pass, in which case every actual-usage
    field on the result is `None`, never substituted with 0.
    """
    if not node_snapshots:
        return None

    usage_by_node = {sample.node_name: sample for sample in (node_usage or [])}
    nodes = [_node_overcommitment(node, settings, usage_by_node.get(node.name)) for node in node_snapshots]

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
    # Same "every Node must have a value, or the cluster-wide number is
    # unknown, never a partial sum" discipline as the three sums above --
    # a Node this function has no usage sample for must not be silently
    # excluded from the total.
    actual_cpu_values = [n.actual_cpu_millicores for n in nodes]
    actual_memory_values = [n.actual_memory_bytes for n in nodes]
    cluster_actual = ResourceQuantity(
        cpu_millicores=None if any(v is None for v in actual_cpu_values) else sum(v for v in actual_cpu_values if v is not None),
        memory_bytes=None
        if any(v is None for v in actual_memory_values)
        else sum(v for v in actual_memory_values if v is not None),
    )

    threshold = settings.optimization_overcommitment_limits_ratio_threshold
    pressure_threshold = settings.optimization_node_pressure_ratio_threshold
    cluster_limits_cpu_ratio = _ratio(cluster_limits.cpu_millicores, cluster_allocatable.cpu_millicores)
    cluster_limits_memory_ratio = _ratio(cluster_limits.memory_bytes, cluster_allocatable.memory_bytes)
    cluster_actual_cpu_ratio = _ratio(cluster_actual.cpu_millicores, cluster_allocatable.cpu_millicores)
    cluster_actual_memory_ratio = _ratio(cluster_actual.memory_bytes, cluster_allocatable.memory_bytes)

    return ClusterOvercommitmentSummary(
        computed_at=now or datetime.now(timezone.utc),
        nodes=nodes,
        cluster_requested_cpu_ratio=_ratio(cluster_requested.cpu_millicores, cluster_allocatable.cpu_millicores),
        cluster_requested_memory_ratio=_ratio(cluster_requested.memory_bytes, cluster_allocatable.memory_bytes),
        cluster_limits_cpu_ratio=cluster_limits_cpu_ratio,
        cluster_limits_memory_ratio=cluster_limits_memory_ratio,
        overcommitted_cpu=cluster_limits_cpu_ratio is not None and cluster_limits_cpu_ratio > threshold,
        overcommitted_memory=cluster_limits_memory_ratio is not None and cluster_limits_memory_ratio > threshold,
        cluster_actual_cpu_ratio=cluster_actual_cpu_ratio,
        cluster_actual_memory_ratio=cluster_actual_memory_ratio,
        under_pressure_cpu=cluster_actual_cpu_ratio is not None and cluster_actual_cpu_ratio > pressure_threshold,
        under_pressure_memory=cluster_actual_memory_ratio is not None
        and cluster_actual_memory_ratio > pressure_threshold,
    )

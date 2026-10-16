"""Capacity planning & autoscaling advisor (Milestone 5 of the platform
spec). Extends the optimization engine rather than duplicating it -- see
docs/CAPACITY_PLANNING.md for the full design, including what's
deliberately out of scope (an external-dependency/latency signal this
project has no evidence source for; a true per-constraint scheduling
simulation; any apply/write path).

`analyze_capacity()` is the one public entry point, mirroring
`analyzer.py::analyze_workload()`'s shape: a pure function over
already-fetched facts (a `WorkloadSnapshot` with its `hpa` field already
attached, this call's fresh per-Pod usage samples, and the cluster's real
Node headroom), returning at most one `ResourceRecommendation` per
workload -- replica capacity is a workload-level/Pod-level concern, not a
per-container one, the same reasoning `analyzer.py::_scheduling_recommendation`
already applies by using `workload.containers[0]` as the representative
container for a workload-level finding.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from kubernetes.client import V2HorizontalPodAutoscaler

from kubesentinel.config import Settings
from kubesentinel.models.optimization import (
    EvidenceQuality,
    HpaMetricStatus,
    HpaSnapshot,
    RecommendationCategory,
    ResourceQuantity,
    ResourceRecommendation,
    TrendDirection,
    UsageSummary,
    WorkloadSnapshot,
)
from kubesentinel.models.scheduling import NodeSnapshot
from kubesentinel.observer.client import ClusterClients
from kubesentinel.optimization.metrics_providers import RawUsageSample
from kubesentinel.optimization.quantities import format_cpu_millicores

logger = logging.getLogger(__name__)


def _insufficient_usage(now: datetime) -> UsageSummary:
    return UsageSummary(evidence_quality=EvidenceQuality.INSUFFICIENT, observed_at=now)


def _hpa_to_snapshot(hpa: V2HorizontalPodAutoscaler) -> HpaSnapshot:
    spec = hpa.spec
    status = hpa.status

    metrics: list[HpaMetricStatus] = []
    spec_by_name: dict[str, object] = {}
    for metric_spec in spec.metrics or []:
        if metric_spec.type == "Resource" and metric_spec.resource is not None:
            spec_by_name[metric_spec.resource.name] = metric_spec.resource.target

    for metric_status in status.current_metrics or []:
        if metric_status.type == "Resource" and metric_status.resource is not None:
            name = metric_status.resource.name
            current = metric_status.resource.current
            target = spec_by_name.get(name)
            metrics.append(
                HpaMetricStatus(
                    metric_name=name,
                    metric_type=metric_status.type,
                    target_utilization_percent=getattr(target, "average_utilization", None),
                    target_average_value=getattr(target, "average_value", None),
                    current_utilization_percent=current.average_utilization if current else None,
                    current_average_value=current.average_value if current else None,
                )
            )
        else:
            # Pods/Object/External/ContainerResource metrics: still reported
            # (never silently dropped), just without the Resource-specific
            # utilization fields -- this engine's own "over target" headroom
            # check (see analyze_capacity) only ever evaluates Resource/
            # Utilization metrics, documented in CAPACITY_PLANNING.md §2.
            metrics.append(HpaMetricStatus(metric_name=metric_status.type, metric_type=metric_status.type))

    scaling_limited = False
    scaling_limited_reason = None
    for condition in status.conditions or []:
        if condition.type == "ScalingLimited" and condition.status == "True":
            scaling_limited = True
            scaling_limited_reason = condition.reason

    return HpaSnapshot(
        hpa_name=hpa.metadata.name,
        namespace=hpa.metadata.namespace,
        scale_target_kind=spec.scale_target_ref.kind,
        scale_target_name=spec.scale_target_ref.name,
        min_replicas=spec.min_replicas,
        max_replicas=spec.max_replicas,
        current_replicas=status.current_replicas or 0,
        desired_replicas=status.desired_replicas or 0,
        metrics=metrics,
        scaling_limited=scaling_limited,
        scaling_limited_reason=scaling_limited_reason,
    )


def build_hpa_inventory(clients: ClusterClients, namespaces: list[str] | None = None) -> list[HpaSnapshot]:
    """One-shot HPA LIST, the same shape `inventory.py::build_workload_inventory`
    already uses -- no watch loop."""
    namespaces = namespaces or []
    if namespaces:
        hpas = []
        for namespace in namespaces:
            hpas.extend(clients.autoscaling.list_namespaced_horizontal_pod_autoscaler(namespace=namespace).items)
    else:
        hpas = clients.autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces().items
    return [_hpa_to_snapshot(hpa) for hpa in hpas]


def attach_hpa_snapshots(workloads: list[WorkloadSnapshot], hpas: list[HpaSnapshot]) -> None:
    """Pure, mutates `workloads` in place -- matches each HPA to the
    workload it targets by (namespace, kind, name), the only key an HPA's
    `scaleTargetRef` carries (no UID). A workload with no matching HPA
    keeps `hpa=None`; an HPA matching no known workload (its target was
    deleted, or isn't a kind this engine inventories) is silently not
    attached anywhere -- there is nothing to attach it to."""
    by_target = {(h.namespace, h.scale_target_kind, h.scale_target_name): h for h in hpas}
    for workload in workloads:
        match = by_target.get((workload.namespace, workload.workload_kind.value, workload.workload_name))
        if match is not None:
            workload.hpa = match


def compute_trend(values: list[float], settings: Settings) -> TrendDirection:
    """A simple two-half-mean comparison over already-time-sorted `values`
    -- explicitly a description of what was observed, never a forecast.
    See docs/CAPACITY_PLANNING.md §2."""
    if len(values) < settings.capacity_trend_min_samples:
        return TrendDirection.INSUFFICIENT
    mid = len(values) // 2
    first_half, second_half = values[:mid], values[mid:]
    if not first_half or not second_half:
        return TrendDirection.INSUFFICIENT
    first_mean = sum(first_half) / len(first_half)
    second_mean = sum(second_half) / len(second_half)
    if first_mean <= 0:
        return TrendDirection.INCREASING if second_mean > 0 else TrendDirection.INSUFFICIENT
    change = (second_mean - first_mean) / first_mean
    if change >= settings.capacity_trend_change_ratio:
        return TrendDirection.INCREASING
    if change <= -settings.capacity_trend_change_ratio:
        return TrendDirection.DECREASING
    return TrendDirection.STABLE


def _sorted_values(samples: list[RawUsageSample], attr: str) -> list[float]:
    ordered = sorted(samples, key=lambda s: s.timestamp)
    return [float(getattr(s, attr)) for s in ordered]


def _window_seconds(samples: list[RawUsageSample]) -> float:
    if len(samples) < 2:
        return 0.0
    timestamps = [s.timestamp for s in samples]
    return (max(timestamps) - min(timestamps)).total_seconds()


def detect_bottleneck_pattern(
    container_name: str, fresh_samples: list[RawUsageSample], current_limit_millicores: int | None, settings: Settings
) -> bool:
    """A narrow pattern match on one specific signal: at least
    `capacity_min_replicas_for_bottleneck_check` distinct Pods, in this
    call's own fresh per-Pod snapshot (never the cross-time pooled bag
    `analyzer.py` uses for sizing -- the point here is distinguishing
    *individual* replicas, which pooling across replicas deliberately
    discards for that other use case), each independently reporting CPU
    usage clustered in a narrow band around one full core, while the
    container's own CPU limit allows meaningfully more. Never a
    profiler-level claim -- see docs/CAPACITY_PLANNING.md §2/§11."""
    if current_limit_millicores is None or current_limit_millicores < (
        settings.capacity_single_core_ceiling_millicores * settings.capacity_min_limit_ratio_for_bottleneck_check
    ):
        return False

    ceiling = settings.capacity_single_core_ceiling_millicores
    band = settings.capacity_single_core_band_millicores
    plateaued_pods = {
        s.pod_name
        for s in fresh_samples
        if s.container_name == container_name and (ceiling - band) <= s.cpu_millicores <= (ceiling + band)
    }
    return len(plateaued_pods) >= settings.capacity_min_replicas_for_bottleneck_check


def compute_scale_headroom(
    node_snapshots: list[NodeSnapshot], per_replica_request: ResourceQuantity, additional_replicas: int
) -> bool:
    """A coarse, explicitly-approximate greedy first-fit against real Node
    allocatable-minus-requested totals -- never a claim equivalent to the
    scheduling advisor's own per-constraint simulation (taints, affinity,
    and quotas are not considered at all here). Returns whether
    `additional_replicas` more Pods, each needing `per_replica_request`,
    could *all* be placed somewhere -- one replica per Node slot found,
    never spread across multiple Nodes' leftover slivers. See
    docs/CAPACITY_PLANNING.md §2/§11."""
    if additional_replicas <= 0:
        return True

    need_cpu = per_replica_request.cpu_millicores or 0
    need_memory = per_replica_request.memory_bytes or 0

    spare = [
        (
            (node.allocatable.cpu_millicores or 0) - (node.requested.cpu_millicores or 0),
            (node.allocatable.memory_bytes or 0) - (node.requested.memory_bytes or 0),
        )
        for node in node_snapshots
        if node.ready and not node.unschedulable
    ]

    placed = 0
    for _ in range(additional_replicas):
        for i, (spare_cpu, spare_memory) in enumerate(spare):
            if spare_cpu >= need_cpu and spare_memory >= need_memory:
                spare[i] = (spare_cpu - need_cpu, spare_memory - need_memory)
                placed += 1
                break
    return placed >= additional_replicas


def _hpa_status_finding(
    workload: WorkloadSnapshot, hpa: HpaSnapshot, node_snapshots: list[NodeSnapshot], now: datetime, settings: Settings
) -> tuple[str, list[str], list[str], list[str]]:
    """Returns (reasoning_sentence, benefits, risks, flags) for the HPA's
    own status -- always produced when an HPA targets this workload,
    whether or not anything is currently wrong (visibility into a healthy
    HPA is itself useful, matching the scheduling advisor's own precedent
    of reporting a passing constraint, not just a failing one)."""
    flags: list[str] = []
    risks: list[str] = []
    reasoning = (
        f"HPA '{hpa.hpa_name}' targets this workload: {hpa.current_replicas} current / "
        f"{hpa.desired_replicas} desired replicas (bounds {hpa.min_replicas}-{hpa.max_replicas})."
    )

    over_target_resource_metrics = [
        m
        for m in hpa.metrics
        if m.metric_type == "Resource"
        and m.target_utilization_percent is not None
        and m.current_utilization_percent is not None
        and m.current_utilization_percent > m.target_utilization_percent
    ]

    at_max = hpa.current_replicas >= hpa.max_replicas
    if at_max and over_target_resource_metrics:
        flags.append("HPA_AT_MAX")
        metric_text = ", ".join(
            f"{m.metric_name} at {m.current_utilization_percent}% (target {m.target_utilization_percent}%)"
            for m in over_target_resource_metrics
        )
        reasoning += (
            f" It is already at its configured maximum ({hpa.max_replicas}) and still over target on: {metric_text}."
        )
        risks.append("This HPA cannot scale further under its current maxReplicas even though demand still exceeds target.")

        container = workload.containers[0] if workload.containers else None
        per_replica_request = container.requests if container else ResourceQuantity()
        if (per_replica_request.cpu_millicores or per_replica_request.memory_bytes) and node_snapshots:
            would_fit = compute_scale_headroom(node_snapshots, per_replica_request, additional_replicas=1)
            if not would_fit:
                flags.append("SCALING_WOULD_BE_UNSCHEDULABLE")
                reasoning += (
                    " Even if maxReplicas were raised, this cluster's current Node allocatable capacity has no "
                    "room for one more replica at this workload's own per-Pod request (a coarse, cluster-wide "
                    "approximation -- not a full scheduling simulation; see docs/CAPACITY_PLANNING.md)."
                )
                risks.append("Raising maxReplicas alone would not help -- the additional replica(s) would be Pending.")
    elif hpa.current_replicas <= (hpa.min_replicas or 1) and hpa.desired_replicas <= (hpa.min_replicas or 1):
        flags.append("HPA_AT_MIN")

    benefits = (
        ["Autoscaling is actively managing this workload's replica count based on observed demand."]
        if not flags
        else []
    )
    return reasoning, benefits, risks, flags


def _missing_requests_for_autoscaling(
    workload: WorkloadSnapshot, hpa: HpaSnapshot
) -> tuple[str, list[str]] | None:
    """HPA Utilization metrics are a percentage *of the configured
    request*, per the HPA's own documented semantics -- a container with
    no request for that resource makes the percentage meaningless. Checked
    deterministically from the HPA spec and the containers' own requests,
    never a guess."""
    missing_for: list[str] = []
    for metric in hpa.metrics:
        if metric.metric_type != "Resource" or metric.target_utilization_percent is None:
            continue
        for container in workload.containers:
            has_request = (
                container.requests.cpu_millicores is not None
                if metric.metric_name == "cpu"
                else container.requests.memory_bytes is not None
                if metric.metric_name == "memory"
                else True
            )
            if not has_request:
                missing_for.append(f"{container.container_name} ({metric.metric_name})")
    if not missing_for:
        return None
    return (
        f"HPA '{hpa.hpa_name}' uses a Utilization target, which is computed as a percentage of the configured "
        f"request -- but {', '.join(missing_for)} has no request for that resource, making this HPA's scaling "
        "decisions meaningless until a request is set.",
        ["MISSING_REQUESTS_FOR_AUTOSCALING_MATH"],
    )


def analyze_capacity(
    workload: WorkloadSnapshot,
    raw_samples: list[RawUsageSample],
    node_snapshots: list[NodeSnapshot],
    settings: Settings,
    now: datetime | None = None,
) -> ResourceRecommendation | None:
    """The one public entry point. Returns `None` only when there is
    genuinely nothing to say: no HPA targets this workload, and neither
    the leak-shaped nor the single-threaded-bottleneck pattern fired on
    any container. `related_incident_ids`/safety suppression are applied
    by the caller (`service.py`) through the same generic
    `safety.py::apply_safety_checks` every other category already goes
    through -- this function never touches that itself."""
    now = now or datetime.now(timezone.utc)

    reasoning_parts: list[str] = []
    benefits: list[str] = []
    risks: list[str] = []
    flags: list[str] = []

    if workload.hpa is not None:
        reasoning, hpa_benefits, hpa_risks, hpa_flags = _hpa_status_finding(workload, workload.hpa, node_snapshots, now, settings)
        reasoning_parts.append(reasoning)
        benefits.extend(hpa_benefits)
        risks.extend(hpa_risks)
        flags.extend(hpa_flags)

        missing = _missing_requests_for_autoscaling(workload, workload.hpa)
        if missing is not None:
            missing_reasoning, missing_flags = missing
            reasoning_parts.append(missing_reasoning)
            flags.extend(missing_flags)

    name_to_uid = dict(zip(workload.pod_names, workload.pod_uids, strict=True))
    fresh_for_workload = [s for s in raw_samples if s.pod_name in name_to_uid]

    for container in workload.containers:
        container_fresh = [s for s in fresh_for_workload if s.container_name == container.container_name]
        memory_values = _sorted_values(container_fresh, "memory_bytes")
        cpu_values = _sorted_values(container_fresh, "cpu_millicores")
        window = _window_seconds(container_fresh)

        memory_trend = compute_trend(memory_values, settings)
        cpu_trend = compute_trend(cpu_values, settings)
        if (
            memory_trend == TrendDirection.INCREASING
            and cpu_trend != TrendDirection.INCREASING
            and window >= settings.capacity_leak_min_window_seconds
        ):
            flags.append("POSSIBLE_MEMORY_LEAK_PATTERN")
            reasoning_parts.append(
                f"Container '{container.container_name}' shows memory usage increasing over the sampled "
                f"{window:.0f}s window while CPU usage is not also increasing -- a pattern consistent with "
                "unbounded growth independent of load (a leak is one explanation, not the only one). More "
                "replicas would delay, not fix, a per-replica leak."
            )
            risks.append("A workload with this pattern can eventually OOM regardless of how many replicas are running.")

        if detect_bottleneck_pattern(
            container.container_name, fresh_for_workload, container.limits.cpu_millicores, settings
        ):
            assert container.limits.cpu_millicores is not None  # guaranteed by detect_bottleneck_pattern's own check
            flags.append("POSSIBLE_SINGLE_THREADED_BOTTLENECK")
            reasoning_parts.append(
                f"Multiple replicas of container '{container.container_name}' independently plateau near "
                f"{format_cpu_millicores(settings.capacity_single_core_ceiling_millicores)} CPU despite a "
                f"configured limit of {format_cpu_millicores(container.limits.cpu_millicores)} -- a pattern "
                "consistent with a per-replica ceiling (e.g. single-threaded work) rather than available CPU "
                "actually being the constraint. This project has no profiling/APM evidence to confirm the cause."
            )
            risks.append("Adding replicas may help (more independent ceilings running in parallel) but will not raise any one replica's own ceiling.")

    if not reasoning_parts:
        return None

    # A healthy, unremarkable HPA (no pressure, no missing requests, no
    # pattern) still reaches here with its one status sentence already in
    # reasoning_parts -- never silenced just because nothing is wrong; see
    # docs/CAPACITY_PLANNING.md §10's "status-only finding" case.
    reasoning_parts.append(
        "This is a basic trend/status analysis over the sampled window, not a statistical forecast -- see "
        "docs/CAPACITY_PLANNING.md for what is and isn't evaluated (notably: no external-dependency/latency "
        "evidence exists in this project at all)."
    )

    container_name = workload.containers[0].container_name if workload.containers else ""
    requests = workload.containers[0].requests if workload.containers else ResourceQuantity()
    limits = workload.containers[0].limits if workload.containers else ResourceQuantity()
    usage = workload.containers[0].usage if workload.containers else None

    return ResourceRecommendation(
        recommendation_id=str(uuid4()),
        namespace=workload.namespace,
        workload_kind=workload.workload_kind,
        workload_name=workload.workload_name,
        container_name=container_name,
        category=RecommendationCategory.CAPACITY,
        current_requests=requests,
        current_limits=limits,
        usage=usage if usage is not None else _insufficient_usage(now),
        reasoning=" ".join(reasoning_parts),
        potential_benefits=benefits,
        risks=risks,
        safety_flags=flags,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.optimization_recommendation_ttl_seconds),
    )

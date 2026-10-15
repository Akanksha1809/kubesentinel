"""Deterministic CPU/memory right-sizing analysis. No ML, no AI call -- every
number here is reproducible from its inputs and its `reasoning` is plain text
an engineer can audit without consulting a model.

`analyze_workload()` is the one public entry point: it attaches a real
`UsageSummary` to each container (from fresh metrics-server samples, this
project's own bounded in-memory history, and optionally a real Prometheus
range query), then runs the CPU/memory/data-quality/scheduling checks below,
then hands the result through `safety.py` before returning it. Nothing here
mutates the cluster or a repository -- see
`tests/integration/test_optimization_inventory_live.py`'s sibling-in-spirit
read-only check and RESOURCE_OPTIMIZATION_DESIGN.md §6.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from kubesentinel.config import Settings
from kubesentinel.diagnostics.rules.scheduling import classify_unschedulable_message
from kubesentinel.models.diagnosis import ReasonCode
from kubesentinel.models.optimization import (
    ContainerResourceSnapshot,
    EvidenceQuality,
    RecommendationCategory,
    ResourceQuantity,
    ResourceRecommendation,
    UsageSummary,
    WorkloadSnapshot,
)
from kubesentinel.optimization.metrics_providers import (
    InMemorySampleStore,
    MetricsProviderError,
    PrometheusProvider,
    RawUsageSample,
)
from kubesentinel.optimization.quantities import format_cpu_millicores, format_memory_bytes
from kubesentinel.optimization.safety import apply_safety_checks

logger = logging.getLogger(__name__)

_RESIZE_ELIGIBLE = (EvidenceQuality.LIMITED_HISTORY, EvidenceQuality.HISTORICAL)


def _percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolation percentile (same method `numpy.percentile`'s
    default uses) -- no numpy dependency for one small, well-tested function."""
    if not values:
        raise ValueError("cannot compute a percentile of an empty list")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = fraction * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _pool_samples_for_container(
    workload: WorkloadSnapshot,
    container_name: str,
    raw_samples: list[RawUsageSample],
    sample_store: InMemorySampleStore | None,
) -> list[RawUsageSample]:
    """Records this call's fresh metrics-server samples into `sample_store`
    (so the *next* call sees more history), then returns every sample --
    old and new -- pooled across every Pod currently backing this workload.
    Pooling across replicas, not per-Pod, is deliberate: a recommendation
    targets the shared container template, so "typical/peak demand" means
    demand across the whole workload, not one arbitrarily-chosen replica."""
    name_to_uid = dict(zip(workload.pod_names, workload.pod_uids, strict=True))
    fresh = [
        sample
        for sample in raw_samples
        if sample.container_name == container_name and sample.pod_name in name_to_uid
    ]
    if sample_store is not None:
        for sample in fresh:
            sample_store.add(name_to_uid[sample.pod_name], sample)
        pooled: list[RawUsageSample] = []
        for pod_uid in workload.pod_uids:
            pooled.extend(sample_store.get(workload.namespace, pod_uid, container_name))
        return pooled
    return fresh


def _summarize_from_samples(pooled: list[RawUsageSample], settings: Settings, now: datetime) -> UsageSummary:
    if not pooled:
        return UsageSummary.insufficient(observed_at=now)

    cpu_values = [float(s.cpu_millicores) for s in pooled]
    memory_values = [float(s.memory_bytes) for s in pooled]
    timestamps = [s.timestamp for s in pooled]
    window_seconds = (max(timestamps) - min(timestamps)).total_seconds()

    enough_for_history = (
        len(pooled) >= settings.optimization_min_samples_for_history
        and window_seconds >= settings.optimization_min_window_seconds_for_history
    )

    return UsageSummary(
        evidence_quality=EvidenceQuality.LIMITED_HISTORY if enough_for_history else EvidenceQuality.SNAPSHOT_ONLY,
        sample_count=len(pooled),
        window_seconds=window_seconds,
        observed_at=now,
        cpu_typical_millicores=round(_percentile(cpu_values, settings.optimization_cpu_typical_percentile)),
        cpu_peak_millicores=round(max(cpu_values)),
        memory_typical_bytes=round(_percentile(memory_values, 0.5)),
        memory_peak_bytes=round(_percentile(memory_values, settings.optimization_memory_peak_percentile)),
        throttled=None,  # no throttling signal without Prometheus cpu.cfs_throttled-style metrics
    )


def _try_prometheus_usage(
    workload: WorkloadSnapshot, container_name: str, settings: Settings, now: datetime
) -> UsageSummary | None:
    """Best-effort upgrade to real historical evidence. Returns `None` on any
    failure (unreachable, no data, ambiguous query) -- the caller falls back
    to the in-memory-sample-based summary; Prometheus being absent or
    misconfigured must never raise out of `analyze_workload()`."""
    if not settings.optimization_prometheus_url or not workload.pod_names:
        return None

    pod_pattern = "|".join(re.escape(name) for name in workload.pod_names)
    provider = PrometheusProvider(settings.optimization_prometheus_url, settings.optimization_prometheus_timeout_seconds)
    window_seconds = max(settings.optimization_min_window_seconds_for_history, 3600.0)
    start = now - timedelta(seconds=window_seconds)
    namespace = workload.namespace

    cpu_query = (
        f'avg(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",'
        f'pod=~"{pod_pattern}",container="{container_name}"}}[5m])) * 1000'
    )
    memory_query = (
        f'avg(container_memory_working_set_bytes{{namespace="{namespace}",'
        f'pod=~"{pod_pattern}",container="{container_name}"}})'
    )
    try:
        cpu_series = provider.query_range(cpu_query, start, now, step_seconds=60)
        memory_series = provider.query_range(memory_query, start, now, step_seconds=60)
    except MetricsProviderError as exc:
        logger.info("Prometheus usage query unavailable, falling back to snapshot-based evidence: %s", exc)
        return None

    if not cpu_series or not memory_series:
        return None

    cpu_values = [value for _, value in cpu_series]
    memory_values = [value for _, value in memory_series]
    actual_window = (cpu_series[-1][0] - cpu_series[0][0]).total_seconds()

    return UsageSummary(
        evidence_quality=EvidenceQuality.HISTORICAL,
        sample_count=len(cpu_series),
        window_seconds=actual_window,
        observed_at=now,
        cpu_typical_millicores=round(_percentile(cpu_values, settings.optimization_cpu_typical_percentile)),
        cpu_peak_millicores=round(max(cpu_values)),
        memory_typical_bytes=round(_percentile(memory_values, 0.5)),
        memory_peak_bytes=round(_percentile(memory_values, settings.optimization_memory_peak_percentile)),
        throttled=None,
    )


def _new_recommendation(
    workload: WorkloadSnapshot,
    container_name: str,
    category: RecommendationCategory,
    usage: UsageSummary,
    current_requests: ResourceQuantity,
    current_limits: ResourceQuantity,
    reasoning: str,
    now: datetime,
    settings: Settings,
    *,
    proposed_requests: ResourceQuantity | None = None,
    proposed_limits: ResourceQuantity | None = None,
    potential_benefits: list[str] | None = None,
    risks: list[str] | None = None,
    safety_flags: list[str] | None = None,
) -> ResourceRecommendation:
    return ResourceRecommendation(
        recommendation_id=str(uuid4()),
        namespace=workload.namespace,
        workload_kind=workload.workload_kind,
        workload_name=workload.workload_name,
        container_name=container_name,
        category=category,
        current_requests=current_requests,
        current_limits=current_limits,
        usage=usage,
        proposed_requests=proposed_requests,
        proposed_limits=proposed_limits,
        reasoning=reasoning,
        potential_benefits=potential_benefits or [],
        risks=risks or [],
        safety_flags=safety_flags or [],
        created_at=now,
        expires_at=now + timedelta(seconds=settings.optimization_recommendation_ttl_seconds),
    )


def _data_quality_recommendation(
    workload: WorkloadSnapshot, container: ContainerResourceSnapshot, now: datetime, settings: Settings
) -> ResourceRecommendation | None:
    missing = []
    flags = []
    if container.requests.cpu_millicores is None:
        missing.append("CPU request")
        flags.append("MISSING_CPU_REQUEST")
    if container.requests.memory_bytes is None:
        missing.append("memory request")
        flags.append("MISSING_MEMORY_REQUEST")
    if container.limits.cpu_millicores is None:
        missing.append("CPU limit")
        flags.append("MISSING_CPU_LIMIT")
    if container.limits.memory_bytes is None:
        missing.append("memory limit")
        flags.append("MISSING_MEMORY_LIMIT")
    if not missing:
        return None

    return _new_recommendation(
        workload,
        container.container_name,
        RecommendationCategory.DATA_QUALITY,
        container.usage,
        container.requests,
        container.limits,
        reasoning=(
            f"This container is missing: {', '.join(missing)}. Without these, the scheduler and "
            "kubelet have no basis for placement/eviction decisions for the missing dimension(s), "
            "and this analysis cannot assess waste or pressure where data is missing -- a gap, not "
            "a measured waste claim."
        ),
        now=now,
        settings=settings,
        risks=[
            "A missing request lets the scheduler place this workload where it may not actually fit.",
            "A missing limit lets this container consume resources without bound on its node.",
        ],
        safety_flags=flags,
    )


def _cpu_recommendation(
    workload: WorkloadSnapshot, container: ContainerResourceSnapshot, now: datetime, settings: Settings
) -> ResourceRecommendation | None:
    usage = container.usage
    current_request = container.requests.cpu_millicores
    current_limit = container.limits.cpu_millicores

    if current_request is None or usage.evidence_quality == EvidenceQuality.INSUFFICIENT:
        return None

    near_limit = (
        current_limit is not None
        and usage.cpu_peak_millicores is not None
        and usage.cpu_peak_millicores >= current_limit * settings.optimization_near_limit_ratio
    )

    proposed_millicores = None
    oversized = False
    if usage.evidence_quality in _RESIZE_ELIGIBLE and usage.cpu_typical_millicores is not None:
        proposed_millicores = max(
            round(usage.cpu_typical_millicores * (1 + settings.optimization_cpu_safety_margin)),
            settings.optimization_cpu_floor_millicores,
        )
        oversized = proposed_millicores < current_request * settings.optimization_underutilization_ratio

    if not oversized and not near_limit:
        return None

    flags, benefits, risks, reasoning_parts = [], [], [], []
    if oversized:
        assert usage.cpu_typical_millicores is not None  # guaranteed by the `oversized` branch above
        flags.append("CPU_OVERSIZED")
        reasoning_parts.append(
            f"Observed p{int(settings.optimization_cpu_typical_percentile * 100)} CPU demand is "
            f"{format_cpu_millicores(usage.cpu_typical_millicores)} over {usage.sample_count} samples "
            f"spanning {usage.window_seconds:.0f}s -- well below the current request of "
            f"{format_cpu_millicores(current_request)}."
        )
        benefits.append("Lowering the CPU request toward observed demand frees reserved capacity for other workloads.")
        risks.append("If real load exceeds what this window observed, the lowered request could under-provision.")
    if near_limit:
        assert usage.cpu_peak_millicores is not None and current_limit is not None  # guaranteed by `near_limit` above
        flags.append("CPU_PRESSURE")
        reasoning_parts.append(
            f"Peak observed CPU usage ({format_cpu_millicores(usage.cpu_peak_millicores)}) is within "
            f"{round((1 - settings.optimization_near_limit_ratio) * 100)}% of the configured limit "
            f"({format_cpu_millicores(current_limit)})."
        )
        risks.append("Usage this close to the limit risks CPU throttling under any additional load.")

    return _new_recommendation(
        workload,
        container.container_name,
        RecommendationCategory.CPU,
        usage,
        container.requests,
        container.limits,
        reasoning=" ".join(reasoning_parts),
        now=now,
        settings=settings,
        proposed_requests=ResourceQuantity(cpu_millicores=proposed_millicores) if oversized else None,
        potential_benefits=benefits,
        risks=risks,
        safety_flags=flags,
    )


def _memory_recommendation(
    workload: WorkloadSnapshot, container: ContainerResourceSnapshot, now: datetime, settings: Settings
) -> ResourceRecommendation | None:
    usage = container.usage
    current_request = container.requests.memory_bytes
    current_limit = container.limits.memory_bytes

    if current_request is None or usage.evidence_quality == EvidenceQuality.INSUFFICIENT:
        return None if not container.recent_oom_detected else _oom_only_recommendation(
            workload, container, now, settings
        )

    near_limit = (
        current_limit is not None
        and usage.memory_peak_bytes is not None
        and usage.memory_peak_bytes >= current_limit * settings.optimization_near_limit_ratio
    )

    proposed_bytes = None
    oversized = False
    if usage.evidence_quality in _RESIZE_ELIGIBLE and usage.memory_peak_bytes is not None:
        proposed_bytes = max(
            round(usage.memory_peak_bytes * (1 + settings.optimization_memory_safety_margin)),
            settings.optimization_memory_floor_bytes,
        )
        oversized = proposed_bytes < current_request * settings.optimization_underutilization_ratio

    # A historical OOM kill increases scrutiny, not confidence in a decrease:
    # never propose lowering memory for a container that has been OOM-killed,
    # regardless of how healthy the most recent window looks -- it may simply
    # be sampled between crashes. This never raises a limit automatically
    # either; it only ever vetoes a proposed *decrease*.
    if container.recent_oom_detected and oversized and proposed_bytes is not None and proposed_bytes < current_request:
        oversized = False
        proposed_bytes = None

    if not oversized and not near_limit and not container.recent_oom_detected:
        return None

    flags, benefits, risks, reasoning_parts = [], [], [], []
    if container.recent_oom_detected:
        flags.append("OOM_HISTORY")
        reasoning_parts.append(
            "This container has a recorded OOMKilled termination. Any memory recommendation here is "
            "held to a higher bar: a decrease is never proposed regardless of recent usage, and an "
            "increase is a human decision this analysis does not make for you."
        )
        risks.append("A workload with OOM history can recur even if recent usage looks moderate.")
    if oversized:
        assert usage.memory_peak_bytes is not None  # guaranteed by the `oversized` branch above
        flags.append("MEMORY_OVERSIZED")
        reasoning_parts.append(
            f"Observed peak memory usage (p{int(settings.optimization_memory_peak_percentile * 100)}) is "
            f"{format_memory_bytes(usage.memory_peak_bytes)} over {usage.sample_count} samples spanning "
            f"{usage.window_seconds:.0f}s -- well below the current request of "
            f"{format_memory_bytes(current_request)}."
        )
        benefits.append("Lowering the memory request toward observed peak frees reserved capacity for other workloads.")
        risks.append("Memory usage can spike outside any observed window; this is based only on what was measured.")
    if near_limit:
        assert usage.memory_peak_bytes is not None and current_limit is not None  # guaranteed by `near_limit` above
        flags.append("MEMORY_PRESSURE")
        reasoning_parts.append(
            f"Peak observed memory usage ({format_memory_bytes(usage.memory_peak_bytes)}) is within "
            f"{round((1 - settings.optimization_near_limit_ratio) * 100)}% of the configured limit "
            f"({format_memory_bytes(current_limit)})."
        )
        risks.append("Usage this close to the limit risks an OOM kill under any additional load.")

    return _new_recommendation(
        workload,
        container.container_name,
        RecommendationCategory.MEMORY,
        usage,
        container.requests,
        container.limits,
        reasoning=" ".join(reasoning_parts),
        now=now,
        settings=settings,
        proposed_requests=ResourceQuantity(memory_bytes=proposed_bytes) if oversized else None,
        potential_benefits=benefits,
        risks=risks,
        safety_flags=flags,
    )


def _oom_only_recommendation(
    workload: WorkloadSnapshot, container: ContainerResourceSnapshot, now: datetime, settings: Settings
) -> ResourceRecommendation:
    """OOM history is worth flagging even with no usable usage evidence at
    all -- the OOM event itself is the evidence; see the spec's "flag
    possible OOM risk when evidence supports it.\""""
    return _new_recommendation(
        workload,
        container.container_name,
        RecommendationCategory.MEMORY,
        container.usage,
        container.requests,
        container.limits,
        reasoning=(
            "This container has a recorded OOMKilled termination and insufficient usage evidence to "
            "assess further. No specific memory change is proposed from this analysis alone."
        ),
        now=now,
        settings=settings,
        risks=["A workload with OOM history can recur; investigate before assuming current limits are adequate."],
        safety_flags=["OOM_HISTORY"],
    )


def _scheduling_recommendation(
    workload: WorkloadSnapshot, now: datetime, settings: Settings
) -> ResourceRecommendation | None:
    if not workload.unschedulable_reason or not workload.containers:
        return None
    reason_code, _confidence, cause = classify_unschedulable_message(workload.unschedulable_reason)
    if reason_code != ReasonCode.PENDING_INSUFFICIENT_RESOURCES:
        return None  # a taint/affinity-caused Pending is not a resource-sizing concern

    container = workload.containers[0]
    return _new_recommendation(
        workload,
        container.container_name,
        RecommendationCategory.SCHEDULING,
        container.usage,
        container.requests,
        container.limits,
        reasoning=(
            f"The scheduler reports this workload is unschedulable: {workload.unschedulable_reason} {cause}"
        ),
        now=now,
        settings=settings,
        potential_benefits=[
            "Lowering the resource request (if it is genuinely oversized for this cluster) or adding "
            "node capacity would let the scheduler place this workload."
        ],
        risks=["This workload has Pods that cannot be scheduled at all -- it is doing no work until this is resolved."],
        safety_flags=["UNSCHEDULABLE"],
    )


def analyze_workload(
    workload: WorkloadSnapshot,
    raw_samples: list[RawUsageSample],
    sample_store: InMemorySampleStore | None,
    settings: Settings,
    now: datetime | None = None,
    related_incident_ids: list[str] | None = None,
) -> list[ResourceRecommendation]:
    """The one public entry point. Attaches real usage evidence to
    `workload`'s containers (mutating them in place -- the caller's
    `WorkloadSnapshot` is the same object this returns recommendations
    about), then runs every deterministic check, then applies
    `safety.py::apply_safety_checks` before returning.

    `related_incident_ids`: currently-open incident IDs already resolved by
    the caller (`service.py`) for this workload -- see
    `safety.py::apply_safety_checks` for why this stays a passed-in fact
    rather than a lookup made from in here."""
    now = now or datetime.now(timezone.utc)
    recommendations: list[ResourceRecommendation] = []

    for container in workload.containers:
        pooled = _pool_samples_for_container(workload, container.container_name, raw_samples, sample_store)
        container.usage = _summarize_from_samples(pooled, settings, now)
        prometheus_usage = _try_prometheus_usage(workload, container.container_name, settings, now)
        if prometheus_usage is not None:
            container.usage = prometheus_usage

        for recommendation in (
            _data_quality_recommendation(workload, container, now, settings),
            _cpu_recommendation(workload, container, now, settings),
            _memory_recommendation(workload, container, now, settings),
        ):
            if recommendation is not None:
                recommendations.append(recommendation)

    scheduling_recommendation = _scheduling_recommendation(workload, now, settings)
    if scheduling_recommendation is not None:
        recommendations.append(scheduling_recommendation)

    return apply_safety_checks(workload, recommendations, settings, related_incident_ids)

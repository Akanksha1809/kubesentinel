from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client import (
    V1ObjectMeta,
    V2CrossVersionObjectReference,
    V2HorizontalPodAutoscaler,
    V2HorizontalPodAutoscalerCondition,
    V2HorizontalPodAutoscalerSpec,
    V2HorizontalPodAutoscalerStatus,
    V2MetricSpec,
    V2MetricStatus,
    V2MetricTarget,
    V2MetricValueStatus,
    V2ResourceMetricSource,
    V2ResourceMetricStatus,
)

from kubesentinel.config import Settings
from kubesentinel.models.optimization import (
    ContainerResourceSnapshot,
    HpaSnapshot,
    ResourceQuantity,
    TrendDirection,
    WorkloadKind,
    WorkloadSnapshot,
)
from kubesentinel.models.scheduling import NodeSnapshot
from kubesentinel.optimization.capacity import (
    _hpa_to_snapshot,
    analyze_capacity,
    attach_hpa_snapshots,
    build_hpa_inventory,
    compute_scale_headroom,
    compute_trend,
    detect_bottleneck_pattern,
)
from kubesentinel.optimization.metrics_providers import RawUsageSample

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
NAMESPACE = "kubesentinel-lab"


def _settings(**overrides) -> Settings:
    defaults = dict(
        capacity_trend_min_samples=4,
        capacity_trend_change_ratio=0.2,
        capacity_leak_min_window_seconds=900.0,
        capacity_single_core_ceiling_millicores=1000,
        capacity_single_core_band_millicores=150,
        capacity_min_replicas_for_bottleneck_check=2,
        capacity_min_limit_ratio_for_bottleneck_check=1.5,
        optimization_recommendation_ttl_seconds=3600.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _workload(
    container_name="app",
    cpu_request=None,
    memory_request=None,
    cpu_limit=None,
    memory_limit=None,
    pod_count=1,
    hpa: HpaSnapshot | None = None,
    workload_kind=WorkloadKind.DEPLOYMENT,
    workload_name="demo",
) -> WorkloadSnapshot:
    pod_uids = [f"pod-uid-{i}" for i in range(pod_count)]
    pod_names = [f"pod-{i}" for i in range(pod_count)]
    return WorkloadSnapshot(
        namespace=NAMESPACE,
        workload_kind=workload_kind,
        workload_name=workload_name,
        workload_uid="workload-uid-1",
        containers=[
            ContainerResourceSnapshot(
                container_name=container_name,
                requests=ResourceQuantity(cpu_millicores=cpu_request, memory_bytes=memory_request),
                limits=ResourceQuantity(cpu_millicores=cpu_limit, memory_bytes=memory_limit),
            )
        ],
        pod_uids=pod_uids,
        pod_names=pod_names,
        desired_replicas=pod_count,
        ready_replicas=pod_count,
        healthy=True,
        hpa=hpa,
    )


def _samples(pod_name, container_name, cpu_values, memory_values, start=None, step_seconds=60):
    start = start or (NOW - timedelta(minutes=len(cpu_values)))
    return [
        RawUsageSample(
            namespace=NAMESPACE,
            pod_name=pod_name,
            container_name=container_name,
            cpu_millicores=cpu,
            memory_bytes=mem,
            timestamp=start + timedelta(seconds=step_seconds * i),
        )
        for i, (cpu, mem) in enumerate(zip(cpu_values, memory_values, strict=True))
    ]


def _hpa(
    name="demo-hpa",
    scale_target_kind="Deployment",
    scale_target_name="demo",
    min_replicas=1,
    max_replicas=5,
    current_replicas=5,
    desired_replicas=5,
    resource_metrics=None,
    scaling_limited=False,
) -> V2HorizontalPodAutoscaler:
    resource_metrics = resource_metrics or []
    metric_specs = []
    metric_statuses = []
    for metric_name, target_pct, current_pct in resource_metrics:
        metric_specs.append(
            V2MetricSpec(
                type="Resource",
                resource=V2ResourceMetricSource(
                    name=metric_name, target=V2MetricTarget(type="Utilization", average_utilization=target_pct)
                ),
            )
        )
        metric_statuses.append(
            V2MetricStatus(
                type="Resource",
                resource=V2ResourceMetricStatus(
                    name=metric_name, current=V2MetricValueStatus(average_utilization=current_pct)
                ),
            )
        )
    conditions = (
        [V2HorizontalPodAutoscalerCondition(type="ScalingLimited", status="True", reason="TooManyReplicas")]
        if scaling_limited
        else []
    )
    return V2HorizontalPodAutoscaler(
        metadata=V1ObjectMeta(name=name, namespace=NAMESPACE),
        spec=V2HorizontalPodAutoscalerSpec(
            scale_target_ref=V2CrossVersionObjectReference(kind=scale_target_kind, name=scale_target_name),
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            metrics=metric_specs,
        ),
        status=V2HorizontalPodAutoscalerStatus(
            current_replicas=current_replicas, desired_replicas=desired_replicas, current_metrics=metric_statuses, conditions=conditions
        ),
    )


class TestHpaToSnapshot:
    def test_converts_basic_fields(self):
        snapshot = _hpa_to_snapshot(_hpa(current_replicas=3, desired_replicas=3, max_replicas=10, min_replicas=2))

        assert snapshot.hpa_name == "demo-hpa"
        assert snapshot.namespace == NAMESPACE
        assert snapshot.scale_target_kind == "Deployment"
        assert snapshot.scale_target_name == "demo"
        assert snapshot.min_replicas == 2
        assert snapshot.max_replicas == 10
        assert snapshot.current_replicas == 3
        assert snapshot.desired_replicas == 3

    def test_converts_resource_metric_current_and_target(self):
        snapshot = _hpa_to_snapshot(_hpa(resource_metrics=[("cpu", 80, 95)]))

        metric = snapshot.metrics[0]
        assert metric.metric_name == "cpu"
        assert metric.metric_type == "Resource"
        assert metric.target_utilization_percent == 80
        assert metric.current_utilization_percent == 95

    def test_scaling_limited_condition_read_verbatim(self):
        snapshot = _hpa_to_snapshot(_hpa(scaling_limited=True))

        assert snapshot.scaling_limited is True
        assert snapshot.scaling_limited_reason == "TooManyReplicas"

    def test_no_scaling_limited_condition_is_false(self):
        snapshot = _hpa_to_snapshot(_hpa(scaling_limited=False))

        assert snapshot.scaling_limited is False
        assert snapshot.scaling_limited_reason is None


class TestBuildHpaInventory:
    def test_lists_all_namespaces_when_none_given(self):
        clients = MagicMock()
        clients.autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = SimpleNamespace(
            items=[_hpa()]
        )

        result = build_hpa_inventory(clients, namespaces=None)

        assert len(result) == 1
        clients.autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces.assert_called_once()

    def test_lists_per_namespace_when_given(self):
        clients = MagicMock()
        clients.autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = SimpleNamespace(items=[_hpa()])

        result = build_hpa_inventory(clients, namespaces=[NAMESPACE])

        assert len(result) == 1
        clients.autoscaling.list_namespaced_horizontal_pod_autoscaler.assert_called_once_with(namespace=NAMESPACE)


class TestAttachHpaSnapshots:
    def test_matches_by_namespace_kind_and_name(self):
        workload = _workload(workload_name="demo")
        hpa_snapshot = _hpa_to_snapshot(_hpa(scale_target_kind="Deployment", scale_target_name="demo"))

        attach_hpa_snapshots([workload], [hpa_snapshot])

        assert workload.hpa is not None
        assert workload.hpa.hpa_name == "demo-hpa"

    def test_no_match_leaves_hpa_none(self):
        workload = _workload(workload_name="other")
        hpa_snapshot = _hpa_to_snapshot(_hpa(scale_target_kind="Deployment", scale_target_name="demo"))

        attach_hpa_snapshots([workload], [hpa_snapshot])

        assert workload.hpa is None

    def test_kind_mismatch_does_not_match(self):
        workload = _workload(workload_name="demo", workload_kind=WorkloadKind.STATEFULSET)
        hpa_snapshot = _hpa_to_snapshot(_hpa(scale_target_kind="Deployment", scale_target_name="demo"))

        attach_hpa_snapshots([workload], [hpa_snapshot])

        assert workload.hpa is None


class TestComputeTrend:
    def test_insufficient_below_min_samples(self):
        assert compute_trend([1.0, 2.0], _settings()) == TrendDirection.INSUFFICIENT

    def test_clearly_increasing(self):
        values = [100.0, 100.0, 200.0, 200.0]
        assert compute_trend(values, _settings()) == TrendDirection.INCREASING

    def test_clearly_decreasing(self):
        values = [200.0, 200.0, 100.0, 100.0]
        assert compute_trend(values, _settings()) == TrendDirection.DECREASING

    def test_stable(self):
        values = [100.0, 102.0, 98.0, 101.0]
        assert compute_trend(values, _settings()) == TrendDirection.STABLE

    def test_zero_baseline_with_increase_is_increasing(self):
        values = [0.0, 0.0, 50.0, 50.0]
        assert compute_trend(values, _settings()) == TrendDirection.INCREASING

    def test_zero_baseline_staying_zero_is_insufficient(self):
        values = [0.0, 0.0, 0.0, 0.0]
        assert compute_trend(values, _settings()) == TrendDirection.INSUFFICIENT


class TestDetectBottleneckPattern:
    def test_fires_when_enough_replicas_plateau_near_ceiling(self):
        samples = _samples("pod-0", "app", [1000, 1000], [1, 1]) + _samples("pod-1", "app", [980, 1020], [1, 1])
        assert detect_bottleneck_pattern("app", samples, current_limit_millicores=2000, settings=_settings()) is True

    def test_does_not_fire_with_too_few_plateaued_replicas(self):
        samples = _samples("pod-0", "app", [1000, 1000], [1, 1])
        assert detect_bottleneck_pattern("app", samples, current_limit_millicores=2000, settings=_settings()) is False

    def test_does_not_fire_without_a_sufficiently_higher_limit(self):
        samples = _samples("pod-0", "app", [1000, 1000], [1, 1]) + _samples("pod-1", "app", [980, 1020], [1, 1])
        assert detect_bottleneck_pattern("app", samples, current_limit_millicores=1000, settings=_settings()) is False

    def test_does_not_fire_with_no_limit_set(self):
        samples = _samples("pod-0", "app", [1000, 1000], [1, 1]) + _samples("pod-1", "app", [980, 1020], [1, 1])
        assert detect_bottleneck_pattern("app", samples, current_limit_millicores=None, settings=_settings()) is False

    def test_does_not_fire_when_usage_spread_out_not_plateaued(self):
        samples = _samples("pod-0", "app", [200, 200], [1, 1]) + _samples("pod-1", "app", [1800, 1800], [1, 1])
        assert detect_bottleneck_pattern("app", samples, current_limit_millicores=2000, settings=_settings()) is False


class TestComputeScaleHeadroom:
    def _node(self, cpu_allocatable, memory_allocatable, cpu_requested, memory_requested, ready=True, unschedulable=False):
        return NodeSnapshot(
            name="node-1",
            allocatable=ResourceQuantity(cpu_millicores=cpu_allocatable, memory_bytes=memory_allocatable),
            requested=ResourceQuantity(cpu_millicores=cpu_requested, memory_bytes=memory_requested),
            ready=ready,
            unschedulable=unschedulable,
        )

    def test_fits_when_spare_capacity_exists(self):
        nodes = [self._node(4000, 8_000_000_000, 1000, 1_000_000_000)]
        assert compute_scale_headroom(nodes, ResourceQuantity(cpu_millicores=500, memory_bytes=500_000_000), 1) is True

    def test_does_not_fit_when_no_spare_capacity(self):
        nodes = [self._node(1000, 1_000_000_000, 1000, 1_000_000_000)]
        assert compute_scale_headroom(nodes, ResourceQuantity(cpu_millicores=100, memory_bytes=100), 1) is False

    def test_zero_additional_replicas_always_fits(self):
        nodes = [self._node(0, 0, 0, 0)]
        assert compute_scale_headroom(nodes, ResourceQuantity(cpu_millicores=1000, memory_bytes=1000), 0) is True

    def test_unschedulable_node_excluded(self):
        nodes = [self._node(4000, 8_000_000_000, 0, 0, unschedulable=True)]
        assert compute_scale_headroom(nodes, ResourceQuantity(cpu_millicores=500, memory_bytes=500), 1) is False

    def test_not_ready_node_excluded(self):
        nodes = [self._node(4000, 8_000_000_000, 0, 0, ready=False)]
        assert compute_scale_headroom(nodes, ResourceQuantity(cpu_millicores=500, memory_bytes=500), 1) is False

    def test_multiple_replicas_consume_spare_cumulatively(self):
        nodes = [self._node(1000, 1_000_000_000, 0, 0)]
        assert compute_scale_headroom(nodes, ResourceQuantity(cpu_millicores=600, memory_bytes=1), 2) is False

    def test_exactly_at_the_boundary_fits(self):
        nodes = [self._node(1000, 1000, 0, 0)]
        assert compute_scale_headroom(nodes, ResourceQuantity(cpu_millicores=1000, memory_bytes=1000), 1) is True


class TestAnalyzeCapacity:
    def test_no_hpa_and_no_pattern_returns_none(self):
        workload = _workload(cpu_limit=2000)
        result = analyze_capacity(workload, [], [], _settings(), now=NOW)
        assert result is None

    def test_healthy_hpa_produces_status_only_finding(self):
        hpa = _hpa_to_snapshot(_hpa(current_replicas=3, max_replicas=10, resource_metrics=[("cpu", 80, 50)]))
        workload = _workload(cpu_request=500, hpa=hpa)

        result = analyze_capacity(workload, [], [], _settings(), now=NOW)

        assert result is not None
        assert result.category.value == "CAPACITY"
        assert result.safety_flags == []
        assert "demo-hpa" in result.reasoning

    def test_hpa_at_max_and_over_target_flags_hpa_at_max(self):
        hpa = _hpa_to_snapshot(_hpa(current_replicas=10, max_replicas=10, resource_metrics=[("cpu", 80, 95)]))
        workload = _workload(cpu_request=500, hpa=hpa)

        result = analyze_capacity(workload, [], [], _settings(), now=NOW)

        assert "HPA_AT_MAX" in result.safety_flags

    def test_hpa_at_max_with_no_cluster_headroom_flags_unschedulable(self):
        hpa = _hpa_to_snapshot(_hpa(current_replicas=10, max_replicas=10, resource_metrics=[("cpu", 80, 95)]))
        workload = _workload(cpu_request=5000, hpa=hpa)
        no_room_node = NodeSnapshot(
            name="node-1",
            allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000),
            requested=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000),
        )

        result = analyze_capacity(workload, [], [no_room_node], _settings(), now=NOW)

        assert "SCALING_WOULD_BE_UNSCHEDULABLE" in result.safety_flags

    def test_hpa_at_max_with_cluster_headroom_does_not_flag_unschedulable(self):
        hpa = _hpa_to_snapshot(_hpa(current_replicas=10, max_replicas=10, resource_metrics=[("cpu", 80, 95)]))
        workload = _workload(cpu_request=500, hpa=hpa)
        plenty_of_room = NodeSnapshot(
            name="node-1",
            allocatable=ResourceQuantity(cpu_millicores=100_000, memory_bytes=100_000_000_000),
            requested=ResourceQuantity(cpu_millicores=0, memory_bytes=0),
        )

        result = analyze_capacity(workload, [], [plenty_of_room], _settings(), now=NOW)

        assert "SCALING_WOULD_BE_UNSCHEDULABLE" not in result.safety_flags

    def test_hpa_at_min_flags_hpa_at_min(self):
        hpa = _hpa_to_snapshot(_hpa(current_replicas=1, min_replicas=1, desired_replicas=1, max_replicas=10))
        workload = _workload(hpa=hpa)

        result = analyze_capacity(workload, [], [], _settings(), now=NOW)

        assert "HPA_AT_MIN" in result.safety_flags

    def test_missing_cpu_request_for_utilization_metric_is_flagged(self):
        hpa = _hpa_to_snapshot(_hpa(resource_metrics=[("cpu", 80, 50)]))
        workload = _workload(cpu_request=None, hpa=hpa)  # no CPU request at all

        result = analyze_capacity(workload, [], [], _settings(), now=NOW)

        assert "MISSING_REQUESTS_FOR_AUTOSCALING_MATH" in result.safety_flags

    def test_present_cpu_request_for_utilization_metric_is_not_flagged(self):
        hpa = _hpa_to_snapshot(_hpa(resource_metrics=[("cpu", 80, 50)]))
        workload = _workload(cpu_request=500, hpa=hpa)

        result = analyze_capacity(workload, [], [], _settings(), now=NOW)

        assert "MISSING_REQUESTS_FOR_AUTOSCALING_MATH" not in result.safety_flags

    def test_memory_leak_pattern_detected_without_any_hpa(self):
        workload = _workload(pod_count=1)
        samples = _samples(
            "pod-0",
            "app",
            cpu_values=[100] * 10,
            memory_values=[100_000_000] * 5 + [300_000_000] * 5,
            start=NOW - timedelta(seconds=1800),
            step_seconds=200,
        )

        result = analyze_capacity(workload, samples, [], _settings(), now=NOW)

        assert result is not None
        assert "POSSIBLE_MEMORY_LEAK_PATTERN" in result.safety_flags

    def test_memory_leak_pattern_not_flagged_when_window_too_short(self):
        workload = _workload(pod_count=1)
        samples = _samples(
            "pod-0",
            "app",
            cpu_values=[100] * 10,
            memory_values=[100_000_000] * 5 + [300_000_000] * 5,
            start=NOW - timedelta(seconds=60),
            step_seconds=6,
        )

        result = analyze_capacity(workload, samples, [], _settings(), now=NOW)

        assert result is None or "POSSIBLE_MEMORY_LEAK_PATTERN" not in result.safety_flags

    def test_memory_leak_pattern_not_flagged_when_cpu_also_increasing(self):
        workload = _workload(pod_count=1)
        samples = _samples(
            "pod-0",
            "app",
            cpu_values=[100] * 5 + [300] * 5,
            memory_values=[100_000_000] * 5 + [300_000_000] * 5,
            start=NOW - timedelta(seconds=1800),
            step_seconds=200,
        )

        result = analyze_capacity(workload, samples, [], _settings(), now=NOW)

        assert result is None or "POSSIBLE_MEMORY_LEAK_PATTERN" not in result.safety_flags

    def test_single_threaded_bottleneck_pattern_detected_without_any_hpa(self):
        workload = _workload(pod_count=2, cpu_limit=2000)
        samples = _samples("pod-0", "app", [1000, 1000], [1, 1]) + _samples("pod-1", "app", [980, 1020], [1, 1])

        result = analyze_capacity(workload, samples, [], _settings(), now=NOW)

        assert result is not None
        assert "POSSIBLE_SINGLE_THREADED_BOTTLENECK" in result.safety_flags

    def test_result_always_has_connectivity_style_caveat_in_reasoning(self):
        hpa = _hpa_to_snapshot(_hpa())
        workload = _workload(hpa=hpa)

        result = analyze_capacity(workload, [], [], _settings(), now=NOW)

        assert "not a statistical forecast" in result.reasoning

    def test_determinism(self):
        hpa = _hpa_to_snapshot(_hpa(current_replicas=10, max_replicas=10, resource_metrics=[("cpu", 80, 95)]))
        workload = _workload(cpu_request=500, hpa=hpa)

        r1 = analyze_capacity(workload, [], [], _settings(), now=NOW)
        r2 = analyze_capacity(workload, [], [], _settings(), now=NOW)

        assert r1.reasoning == r2.reasoning
        assert r1.safety_flags == r2.safety_flags

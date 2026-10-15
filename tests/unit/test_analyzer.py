import json
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kubesentinel.config import Settings
from kubesentinel.models.optimization import (
    ContainerResourceSnapshot,
    EvidenceQuality,
    RecommendationCategory,
    ResourceQuantity,
    WorkloadKind,
    WorkloadSnapshot,
)
from kubesentinel.optimization.analyzer import _percentile, analyze_workload
from kubesentinel.optimization.metrics_providers import InMemorySampleStore, RawUsageSample

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
NAMESPACE = "kubesentinel-lab"


def _settings(**overrides) -> Settings:
    defaults = dict(
        optimization_min_samples_for_history=3,
        optimization_min_window_seconds_for_history=60.0,
        optimization_cpu_typical_percentile=0.95,
        optimization_cpu_safety_margin=0.2,
        optimization_cpu_floor_millicores=10,
        optimization_memory_peak_percentile=0.99,
        optimization_memory_safety_margin=0.15,
        optimization_memory_floor_bytes=1024,
        optimization_underutilization_ratio=0.5,
        optimization_near_limit_ratio=0.85,
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
    healthy=True,
    is_system_namespace=False,
    workload_kind=WorkloadKind.DEPLOYMENT,
    recent_oom_detected=False,
    unschedulable_reason=None,
) -> WorkloadSnapshot:
    pod_uids = [f"pod-uid-{i}" for i in range(pod_count)]
    pod_names = [f"pod-{i}" for i in range(pod_count)]
    return WorkloadSnapshot(
        namespace=NAMESPACE,
        workload_kind=workload_kind,
        workload_name="demo",
        workload_uid="workload-uid-1",
        containers=[
            ContainerResourceSnapshot(
                container_name=container_name,
                requests=ResourceQuantity(cpu_millicores=cpu_request, memory_bytes=memory_request),
                limits=ResourceQuantity(cpu_millicores=cpu_limit, memory_bytes=memory_limit),
                recent_oom_detected=recent_oom_detected,
            )
        ],
        pod_uids=pod_uids,
        pod_names=pod_names,
        desired_replicas=pod_count,
        ready_replicas=pod_count if healthy else 0,
        healthy=healthy,
        is_system_namespace=is_system_namespace,
        unschedulable_reason=unschedulable_reason,
    )


def _samples(pod_name, container_name, cpu_values, memory_values, start=NOW - timedelta(minutes=10), step_seconds=60):
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


class TestPercentile:
    def test_single_value(self):
        assert _percentile([42.0], 0.95) == 42.0

    def test_median_of_odd_count(self):
        assert _percentile([1.0, 2.0, 3.0], 0.5) == 2.0

    def test_p95_matches_numpy_linear_method(self):
        # Hand-computed: 10 values 1..10, p95 -> rank = 0.95*9 = 8.55 -> interpolate between index 8 (9.0) and 9 (10.0)
        values = [float(i) for i in range(1, 11)]
        assert _percentile(values, 0.95) == pytest.approx(9.55)


class TestInsufficientEvidence:
    def test_no_samples_at_all_produces_no_cpu_or_memory_recommendation(self):
        # Fully specified (requests and limits both set) so the only variable
        # under test is the absence of usage evidence, not a data-quality gap.
        workload = _workload(cpu_request=1000, memory_request=1024**3, cpu_limit=2000, memory_limit=2 * 1024**3)
        recommendations = analyze_workload(workload, raw_samples=[], sample_store=InMemorySampleStore(), settings=_settings(), now=NOW)
        assert recommendations == []
        assert workload.containers[0].usage.evidence_quality == EvidenceQuality.INSUFFICIENT

    def test_single_sample_is_snapshot_only_and_never_proposes_a_number(self):
        workload = _workload(cpu_request=1000, memory_request=1024**3)
        samples = _samples("pod-0", "app", [50], [1024])
        recommendations = analyze_workload(workload, raw_samples=samples, sample_store=InMemorySampleStore(), settings=_settings(), now=NOW)

        assert workload.containers[0].usage.evidence_quality == EvidenceQuality.SNAPSHOT_ONLY
        # One snapshot alone must never produce a specific resize number --
        # "do not recommend aggressive downsizing from a single sample."
        assert all(r.proposed_requests is None for r in recommendations)


class TestCpuOversized:
    def test_oversized_cpu_proposes_a_lower_request(self):
        workload = _workload(cpu_request=1000, memory_request=1024**3)
        # Pooled via the sample store over several calls simulates accumulated history.
        store = InMemorySampleStore()
        settings = _settings()
        for i in range(5):
            samples = _samples("pod-0", "app", [100] * 1, [1024] * 1, start=NOW - timedelta(minutes=5 - i))
            analyze_workload(workload, raw_samples=samples, sample_store=store, settings=settings, now=NOW - timedelta(minutes=5 - i))

        recommendations = analyze_workload(workload, raw_samples=[], sample_store=store, settings=settings, now=NOW)
        cpu_recs = [r for r in recommendations if r.category == RecommendationCategory.CPU]

        assert len(cpu_recs) == 1
        rec = cpu_recs[0]
        assert "CPU_OVERSIZED" in rec.safety_flags
        assert rec.proposed_requests is not None
        assert rec.proposed_requests.cpu_millicores == round(100 * 1.2)  # typical(100) * (1+0.2 margin)
        assert rec.usage.evidence_quality == EvidenceQuality.LIMITED_HISTORY

    def test_well_sized_cpu_produces_no_recommendation(self):
        workload = _workload(cpu_request=120, memory_request=1024**3)
        store = InMemorySampleStore()
        settings = _settings()
        for i in range(5):
            samples = _samples("pod-0", "app", [100], [1024], start=NOW - timedelta(minutes=5 - i))
            analyze_workload(workload, raw_samples=samples, sample_store=store, settings=settings, now=NOW - timedelta(minutes=5 - i))

        recommendations = analyze_workload(workload, raw_samples=[], sample_store=store, settings=settings, now=NOW)
        assert [r for r in recommendations if r.category == RecommendationCategory.CPU] == []

    def test_cpu_floor_is_never_undercut(self):
        workload = _workload(cpu_request=1000, memory_request=1024**3)
        store = InMemorySampleStore()
        settings = _settings(optimization_cpu_floor_millicores=50)
        for i in range(5):
            samples = _samples("pod-0", "app", [1], [1024], start=NOW - timedelta(minutes=5 - i))  # near-zero usage
            analyze_workload(workload, raw_samples=samples, sample_store=store, settings=settings, now=NOW - timedelta(minutes=5 - i))

        recommendations = analyze_workload(workload, raw_samples=[], sample_store=store, settings=settings, now=NOW)
        cpu_rec = next(r for r in recommendations if r.category == RecommendationCategory.CPU)
        assert cpu_rec.proposed_requests.cpu_millicores == 50


class TestCpuPressure:
    def test_near_limit_flags_pressure_without_proposing_a_decrease(self):
        workload = _workload(cpu_request=100, cpu_limit=110, memory_request=1024**3)
        samples = _samples("pod-0", "app", [100], [1024])  # single snapshot is enough to flag pressure
        recommendations = analyze_workload(workload, raw_samples=samples, sample_store=InMemorySampleStore(), settings=_settings(), now=NOW)

        cpu_recs = [r for r in recommendations if r.category == RecommendationCategory.CPU]
        assert len(cpu_recs) == 1
        assert "CPU_PRESSURE" in cpu_recs[0].safety_flags
        assert cpu_recs[0].proposed_requests is None


class TestMemoryOomHistory:
    def test_oom_history_vetoes_a_proposed_decrease(self):
        workload = _workload(cpu_request=1000, memory_request=1024 * 1024 * 1024, recent_oom_detected=True)
        store = InMemorySampleStore()
        settings = _settings()
        low_memory = 1024 * 1024  # 1Mi -- would otherwise look very oversized
        for i in range(5):
            samples = _samples("pod-0", "app", [50], [low_memory], start=NOW - timedelta(minutes=5 - i))
            analyze_workload(workload, raw_samples=samples, sample_store=store, settings=settings, now=NOW - timedelta(minutes=5 - i))

        recommendations = analyze_workload(workload, raw_samples=[], sample_store=store, settings=settings, now=NOW)
        memory_recs = [r for r in recommendations if r.category == RecommendationCategory.MEMORY]

        assert len(memory_recs) == 1
        assert "OOM_HISTORY" in memory_recs[0].safety_flags
        assert "MEMORY_OVERSIZED" not in memory_recs[0].safety_flags
        assert memory_recs[0].proposed_requests is None

    def test_oom_history_flagged_even_with_no_usage_evidence(self):
        workload = _workload(cpu_request=1000, memory_request=1024**3, recent_oom_detected=True)
        recommendations = analyze_workload(workload, raw_samples=[], sample_store=InMemorySampleStore(), settings=_settings(), now=NOW)

        memory_recs = [r for r in recommendations if r.category == RecommendationCategory.MEMORY]
        assert len(memory_recs) == 1
        assert "OOM_HISTORY" in memory_recs[0].safety_flags
        assert memory_recs[0].usage.evidence_quality == EvidenceQuality.INSUFFICIENT


class TestDataQuality:
    def test_missing_requests_and_limits_reported_not_silently_dropped(self):
        workload = _workload()  # no requests/limits set at all
        recommendations = analyze_workload(workload, raw_samples=[], sample_store=InMemorySampleStore(), settings=_settings(), now=NOW)

        dq_recs = [r for r in recommendations if r.category == RecommendationCategory.DATA_QUALITY]
        assert len(dq_recs) == 1
        assert set(dq_recs[0].safety_flags) == {
            "MISSING_CPU_REQUEST", "MISSING_MEMORY_REQUEST", "MISSING_CPU_LIMIT", "MISSING_MEMORY_LIMIT",
        }
        assert dq_recs[0].proposed_requests is None

    def test_fully_specified_container_has_no_data_quality_row(self):
        workload = _workload(cpu_request=100, memory_request=1024**2, cpu_limit=200, memory_limit=2 * 1024**2)
        recommendations = analyze_workload(workload, raw_samples=[], sample_store=InMemorySampleStore(), settings=_settings(), now=NOW)

        assert [r for r in recommendations if r.category == RecommendationCategory.DATA_QUALITY] == []


class TestScheduling:
    def test_insufficient_resources_scheduling_failure_is_reported(self):
        workload = _workload(
            cpu_request=100000, memory_request=1024**3,
            unschedulable_reason="0/1 nodes are available: 1 Insufficient cpu, 1 Insufficient memory.",
        )
        recommendations = analyze_workload(workload, raw_samples=[], sample_store=InMemorySampleStore(), settings=_settings(), now=NOW)

        sched_recs = [r for r in recommendations if r.category == RecommendationCategory.SCHEDULING]
        assert len(sched_recs) == 1
        assert "UNSCHEDULABLE" in sched_recs[0].safety_flags

    def test_taint_based_unschedulable_is_not_a_scheduling_resource_recommendation(self):
        workload = _workload(
            cpu_request=100, memory_request=1024**2,
            unschedulable_reason="0/1 nodes are available: 1 node(s) had untolerated taint.",
        )
        recommendations = analyze_workload(workload, raw_samples=[], sample_store=InMemorySampleStore(), settings=_settings(), now=NOW)

        assert [r for r in recommendations if r.category == RecommendationCategory.SCHEDULING] == []

    def test_schedulable_workload_has_no_scheduling_recommendation(self):
        workload = _workload(cpu_request=100, memory_request=1024**2)
        recommendations = analyze_workload(workload, raw_samples=[], sample_store=InMemorySampleStore(), settings=_settings(), now=NOW)

        assert [r for r in recommendations if r.category == RecommendationCategory.SCHEDULING] == []


class TestSafetyIntegration:
    def test_unhealthy_workload_suppresses_proposed_values_but_keeps_the_row(self):
        workload = _workload(cpu_request=1000, memory_request=1024**3, healthy=False)
        store = InMemorySampleStore()
        settings = _settings()
        for i in range(5):
            samples = _samples("pod-0", "app", [100], [1024], start=NOW - timedelta(minutes=5 - i))
            analyze_workload(workload, raw_samples=samples, sample_store=store, settings=settings, now=NOW - timedelta(minutes=5 - i))

        recommendations = analyze_workload(workload, raw_samples=[], sample_store=store, settings=settings, now=NOW)
        cpu_rec = next(r for r in recommendations if r.category == RecommendationCategory.CPU)

        assert "UNHEALTHY_WORKLOAD" in cpu_rec.safety_flags
        assert cpu_rec.proposed_requests is None  # suppressed
        assert "CPU_OVERSIZED" in cpu_rec.safety_flags  # the underlying finding is still visible

    def test_system_namespace_suppresses_proposed_values(self):
        workload = _workload(cpu_request=1000, memory_request=1024**3, is_system_namespace=True)
        store = InMemorySampleStore()
        settings = _settings()
        for i in range(5):
            samples = _samples("pod-0", "app", [100], [1024], start=NOW - timedelta(minutes=5 - i))
            analyze_workload(workload, raw_samples=samples, sample_store=store, settings=settings, now=NOW - timedelta(minutes=5 - i))

        recommendations = analyze_workload(workload, raw_samples=[], sample_store=store, settings=settings, now=NOW)
        cpu_rec = next(r for r in recommendations if r.category == RecommendationCategory.CPU)

        assert "SYSTEM_NAMESPACE" in cpu_rec.safety_flags
        assert cpu_rec.proposed_requests is None

    def test_job_is_treated_conservatively(self):
        workload = _workload(cpu_request=1000, memory_request=1024**3, workload_kind=WorkloadKind.JOB)
        store = InMemorySampleStore()
        settings = _settings()
        for i in range(5):
            samples = _samples("pod-0", "app", [100], [1024], start=NOW - timedelta(minutes=5 - i))
            analyze_workload(workload, raw_samples=samples, sample_store=store, settings=settings, now=NOW - timedelta(minutes=5 - i))

        recommendations = analyze_workload(workload, raw_samples=[], sample_store=store, settings=settings, now=NOW)
        cpu_rec = next(r for r in recommendations if r.category == RecommendationCategory.CPU)

        assert "JOB_CONSERVATIVE" in cpu_rec.safety_flags
        assert cpu_rec.proposed_requests is None

    def test_related_incident_ids_are_threaded_through_to_every_recommendation(self):
        workload = _workload(cpu_request=1000, memory_request=1024**3)
        store = InMemorySampleStore()
        settings = _settings()
        for i in range(5):
            samples = _samples("pod-0", "app", [100], [1024], start=NOW - timedelta(minutes=5 - i))
            analyze_workload(
                workload, raw_samples=samples, sample_store=store, settings=settings, now=NOW - timedelta(minutes=5 - i)
            )

        recommendations = analyze_workload(
            workload, raw_samples=[], sample_store=store, settings=settings, now=NOW, related_incident_ids=["incident-1"]
        )
        cpu_rec = next(r for r in recommendations if r.category == RecommendationCategory.CPU)

        assert "ACTIVE_INCIDENT" in cpu_rec.safety_flags
        assert cpu_rec.related_incident_ids == ["incident-1"]
        assert cpu_rec.proposed_requests is None


class TestMultiReplicaPooling:
    def test_usage_is_pooled_across_all_replicas_not_just_one(self):
        workload = _workload(cpu_request=1000, memory_request=1024**3, pod_count=2)
        store = InMemorySampleStore()
        settings = _settings()
        now = NOW
        for i in range(3):
            t = now - timedelta(minutes=3 - i)
            samples = _samples("pod-0", "app", [100], [1024], start=t) + _samples("pod-1", "app", [200], [2048], start=t)
            analyze_workload(workload, raw_samples=samples, sample_store=store, settings=settings, now=t)

        analyze_workload(workload, raw_samples=[], sample_store=store, settings=settings, now=now)
        assert workload.containers[0].usage.sample_count == 6  # 3 rounds x 2 pods
        # peak across both replicas must be visible (200), not just pod-0's own 100
        assert workload.containers[0].usage.cpu_peak_millicores == 200


class TestPrometheusUpgrade:
    def _prom_body(self, cpu_values, memory_values, start_ts=1700000000, step=60):
        cpu_vals = [[start_ts + step * i, str(v)] for i, v in enumerate(cpu_values)]
        mem_vals = [[start_ts + step * i, str(v)] for i, v in enumerate(memory_values)]
        return {
            "cpu": {"status": "success", "data": {"result": [{"metric": {}, "values": cpu_vals}]}},
            "mem": {"status": "success", "data": {"result": [{"metric": {}, "values": mem_vals}]}},
        }

    def test_prometheus_data_upgrades_evidence_quality_to_historical(self):
        workload = _workload(cpu_request=1000, memory_request=1024**3)
        settings = _settings(optimization_prometheus_url="http://localhost:9090")
        bodies = self._prom_body([0.1, 0.1, 0.1], [1024, 1024, 1024])

        call_count = {"n": 0}

        def fake_urlopen(url, timeout=None):
            call_count["n"] += 1
            body = bodies["cpu"] if call_count["n"] == 1 else bodies["mem"]
            return _FakeHttpResponse(body)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            analyze_workload(workload, raw_samples=[], sample_store=InMemorySampleStore(), settings=settings, now=NOW)

        assert workload.containers[0].usage.evidence_quality == EvidenceQuality.HISTORICAL

    def test_prometheus_failure_falls_back_to_snapshot_based_evidence(self):
        workload = _workload(cpu_request=1000, memory_request=1024**3)
        settings = _settings(optimization_prometheus_url="http://localhost:9090")
        samples = _samples("pod-0", "app", [50], [1024])

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            analyze_workload(workload, raw_samples=samples, sample_store=InMemorySampleStore(), settings=settings, now=NOW)

        # Falls back to the metrics-server/in-memory-sample path -- never raises,
        # never silently loses the evidence that path did gather.
        assert workload.containers[0].usage.evidence_quality == EvidenceQuality.SNAPSHOT_ONLY


class _FakeHttpResponse:
    def __init__(self, body: dict):
        self._data = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self._data

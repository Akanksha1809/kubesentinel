"""Live kind-cluster scenarios for the resource optimization engine
(RESOURCE_OPTIMIZATION_DESIGN.md Section 12's "Live kind-cluster" tier) --
real workloads, real `build_workload_inventory`/`analyze_workload` calls,
never mocked. Where a check genuinely needs several minutes of accumulated
usage samples (LIMITED_HISTORY evidence quality), that's out of reach for a
single pytest run and was instead verified manually against a long-lived
API server process -- see TODO.md's Milestone 18 writeup. What's tested
here is what a single pass can honestly check: real snapshot evidence
collection, the "no proposed number from one sample" guarantee against real
(not mocked) usage data, and the read-only guarantee for the full
`OptimizationService.analyze()` call, not just inventory collection.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kubesentinel.config import Settings
from kubesentinel.models.optimization import RecommendationCategory, WorkloadKind
from kubesentinel.optimization.analyzer import analyze_workload
from kubesentinel.optimization.inventory import build_workload_inventory
from kubesentinel.optimization.metrics_providers import InMemorySampleStore, MetricsApiProvider, MetricsProviderError
from kubesentinel.optimization.service import OptimizationService
from kubesentinel.persistence.optimization_repo import SQLiteOptimizationRepository
from tests.conftest import LAB_NAMESPACE

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")


@pytest.fixture
def oversized_resources_workload(live_cluster_clients):
    snapshots = build_workload_inventory(live_cluster_clients, namespaces=[LAB_NAMESPACE])
    by_name = {s.workload_name: s for s in snapshots}
    if "oversized-resources-demo" not in by_name:
        pytest.skip(
            "no 'oversized-resources-demo' workload found -- run "
            "./lab/scripts/run-scenario.sh oversized-resources first"
        )
    return by_name["oversized-resources-demo"]


@pytest.fixture
def missing_requests_workload(live_cluster_clients):
    snapshots = build_workload_inventory(live_cluster_clients, namespaces=[LAB_NAMESPACE])
    by_name = {s.workload_name: s for s in snapshots}
    if "missing-requests-demo" not in by_name:
        pytest.skip(
            "no 'missing-requests-demo' workload found -- run "
            "./lab/scripts/run-scenario.sh missing-requests first"
        )
    return by_name["missing-requests-demo"]


def test_missing_requests_scenario_produces_data_quality_with_no_proposed_numbers(missing_requests_workload):
    assert missing_requests_workload.workload_kind == WorkloadKind.DEPLOYMENT
    container = missing_requests_workload.containers[0]
    assert container.requests.cpu_millicores is None
    assert container.limits.cpu_millicores is None

    recommendations = analyze_workload(
        missing_requests_workload, raw_samples=[], sample_store=None, settings=Settings()
    )

    assert len(recommendations) == 1
    rec = recommendations[0]
    assert rec.category == RecommendationCategory.DATA_QUALITY
    assert rec.proposed_requests is None
    assert rec.proposed_limits is None


def test_oversized_resources_scenario_has_real_near_zero_usage_when_metrics_available(
    live_cluster_clients, oversized_resources_workload
):
    """Confirms real (not mocked) metrics-server data for a genuinely idle
    container reads as near-zero -- and that a *single* pass over it still
    correctly withholds a proposed number, since one sample never reaches
    LIMITED_HISTORY. Skips outright if metrics-server isn't installed on
    this cluster (see lab/install-metrics-server.sh) -- that's this
    project's documented optional/honest-absence path, not a test failure."""
    provider = MetricsApiProvider(live_cluster_clients.custom_objects, timeout_seconds=10.0)
    try:
        samples = provider.get_current_pod_usage(namespace=LAB_NAMESPACE)
    except MetricsProviderError as exc:
        pytest.skip(f"metrics-server not available on this cluster: {exc}")

    own_samples = [s for s in samples if s.pod_name in oversized_resources_workload.pod_names]
    assert own_samples, "expected at least one real usage sample for oversized-resources-demo"
    # A sleeping busybox container's real CPU usage is a few millicores at
    # most -- nowhere near the 1000m this Deployment requests.
    assert own_samples[0].cpu_millicores < 100

    recommendations = analyze_workload(
        oversized_resources_workload, raw_samples=samples, sample_store=InMemorySampleStore(), settings=Settings()
    )
    for rec in recommendations:
        if rec.category in (RecommendationCategory.CPU, RecommendationCategory.MEMORY):
            assert rec.proposed_requests is None, "a single sample must never produce a proposed number"


def test_full_analyze_pass_never_issues_a_non_get_http_request(live_cluster_clients):
    """The strongest form of the read-only guarantee, extended from
    test_optimization_inventory_live.py's inventory-only check to the
    *entire* `OptimizationService.analyze()` path -- inventory collection,
    the metrics-server call, and persistence -- exactly the check
    RESOURCE_OPTIMIZATION_DESIGN.md Section 12 asks for explicitly.
    Persistence itself is local SQLite, not an HTTP call, so this only needs
    to watch the Kubernetes API client's own transport."""
    import tempfile
    from pathlib import Path

    pool_manager = live_cluster_clients.core.api_client.rest_client.pool_manager
    original_request = pool_manager.request
    methods_seen: set[str] = set()

    def spy(method, url, *args, **kwargs):
        methods_seen.add(method)
        return original_request(method, url, *args, **kwargs)

    with tempfile.TemporaryDirectory() as d:
        repo = SQLiteOptimizationRepository(str(Path(d) / "test.db"))
        try:
            service = OptimizationService(live_cluster_clients, repo, Settings())
            with patch.object(pool_manager, "request", side_effect=spy):
                service.analyze(namespaces=[LAB_NAMESPACE])
        finally:
            repo.close()

    assert methods_seen, "expected at least one HTTP call to have been made"
    assert methods_seen <= {"GET"}, f"a full analyze() pass issued non-GET HTTP method(s): {methods_seen}"

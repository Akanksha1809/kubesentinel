"""Live kind-cluster scenario for the capacity planning & autoscaling
advisor (docs/CAPACITY_PLANNING.md Section 10's "Live" tier). A real
HorizontalPodAutoscaler, a real CPU-bound Pod, real Node allocatable
capacity -- never mocked -- confirms the engine's own HPA-status and
scale-headroom reasoning reaches the right conclusion against actual
cluster state, and that a full analyze() pass never issues a mutating
call against the cluster.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kubesentinel.config import Settings
from kubesentinel.optimization.service import OptimizationService
from kubesentinel.persistence.optimization_repo import SQLiteOptimizationRepository
from tests.conftest import LAB_NAMESPACE

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")

_SCENARIO_DEPLOYMENT = "hpa-capacity-pressure-demo"


@pytest.fixture
def optimization_service(live_cluster_clients):
    with tempfile.TemporaryDirectory() as d:
        repo = SQLiteOptimizationRepository(str(Path(d) / "test.db"))
        try:
            yield OptimizationService(live_cluster_clients, repo, Settings())
        finally:
            repo.close()


def _require_scenario_hpa(live_cluster_clients):
    hpas = live_cluster_clients.autoscaling.list_namespaced_horizontal_pod_autoscaler(namespace=LAB_NAMESPACE).items
    if not any(h.metadata.name == _SCENARIO_DEPLOYMENT for h in hpas):
        pytest.skip(f"'{_SCENARIO_DEPLOYMENT}' HPA not found -- run ./lab/scripts/run-scenario.sh hpa-capacity-pressure first")


def test_hpa_capacity_pressure_scenario_is_flagged_at_max(live_cluster_clients, optimization_service):
    _require_scenario_hpa(live_cluster_clients)

    optimization_service.analyze(namespaces=[LAB_NAMESPACE])

    capacity_recs = optimization_service.list_recommendations(
        namespace=LAB_NAMESPACE, workload_name=_SCENARIO_DEPLOYMENT, category="CAPACITY"
    )
    assert len(capacity_recs) == 1
    assert "HPA_AT_MAX" in capacity_recs[0].safety_flags


def test_hpa_capacity_pressure_scenario_exposes_real_hpa_status(live_cluster_clients, optimization_service):
    _require_scenario_hpa(live_cluster_clients)

    optimization_service.analyze(namespaces=[LAB_NAMESPACE])

    workloads = optimization_service.list_workloads(namespace=LAB_NAMESPACE)
    workload = next(w for w in workloads if w.workload_name == _SCENARIO_DEPLOYMENT)
    assert workload.hpa is not None
    assert workload.hpa.hpa_name == _SCENARIO_DEPLOYMENT
    assert workload.hpa.max_replicas == 1
    assert workload.hpa.current_replicas == 1


def test_full_analyze_pass_never_issues_a_non_get_http_request(live_cluster_clients, optimization_service):
    pool_manager = live_cluster_clients.core.api_client.rest_client.pool_manager
    original_request = pool_manager.request
    methods_seen: set[str] = set()

    def spy(method, url, *args, **kwargs):
        methods_seen.add(method)
        return original_request(method, url, *args, **kwargs)

    with patch.object(pool_manager, "request", side_effect=spy):
        optimization_service.analyze(namespaces=[LAB_NAMESPACE])

    assert methods_seen, "expected at least one HTTP call to have been made"
    assert methods_seen <= {"GET"}, f"a full analyze() pass issued non-GET HTTP method(s): {methods_seen}"

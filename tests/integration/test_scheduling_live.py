"""Live kind-cluster scenarios for the scheduling advisor
(docs/SCHEDULING_ANALYSIS.md Section 10's "Live" tier). Real Nodes, real
Pods, never mocked -- confirms the advisor's own independent constraint
math reaches the same conclusion the real scheduler already did, and that
analysis never issues a mutating call against the cluster.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kubesentinel.models.scheduling import ConstraintType
from kubesentinel.persistence.scheduling_repo import SQLiteSchedulingRepository
from kubesentinel.scheduling.service import SchedulingService
from tests.conftest import LAB_NAMESPACE

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")


@pytest.fixture
def scheduling_service(live_cluster_clients):
    with tempfile.TemporaryDirectory() as d:
        repo = SQLiteSchedulingRepository(str(Path(d) / "test.db"))
        try:
            yield SchedulingService(live_cluster_clients, repo)
        finally:
            repo.close()


def _pod_name(live_cluster_clients, label_selector: str) -> str:
    pods = live_cluster_clients.core.list_namespaced_pod(namespace=LAB_NAMESPACE, label_selector=label_selector)
    if not pods.items:
        pytest.skip(f"no pod matching '{label_selector}' found -- run the matching lab scenario first")
    return pods.items[0].metadata.name


def test_resource_shortfall_scenario_blocks_on_resource_fit(live_cluster_clients, scheduling_service):
    pod_name = _pod_name(live_cluster_clients, "app=pending-resources-demo")

    finding = scheduling_service.analyze_pod(LAB_NAMESPACE, pod_name)

    assert finding.any_node_schedulable is False
    blocked_constraints = {
        cr.constraint_type
        for node_result in finding.node_results
        for cr in node_result.constraint_results
        if not cr.satisfied
    }
    assert ConstraintType.RESOURCE_FIT in blocked_constraints
    assert ConstraintType.NODE_SELECTOR not in blocked_constraints


def test_invalid_node_selector_scenario_blocks_on_node_selector_only(live_cluster_clients, scheduling_service):
    pod_name = _pod_name(live_cluster_clients, "app=invalid-node-selector-demo")

    finding = scheduling_service.analyze_pod(LAB_NAMESPACE, pod_name)

    assert finding.any_node_schedulable is False
    for node_result in finding.node_results:
        results_by_type = {cr.constraint_type: cr.satisfied for cr in node_result.constraint_results}
        assert results_by_type.get(ConstraintType.NODE_SELECTOR) is False
        # The real point of this scenario: it's a selector problem, not a
        # resource one -- confirms the advisor distinguishes *which*
        # constraint blocked placement, not just that scheduling failed.
        assert results_by_type.get(ConstraintType.RESOURCE_FIT) is not False


def test_invalid_node_selector_resolves_the_owning_deployment(live_cluster_clients, scheduling_service):
    pod_name = _pod_name(live_cluster_clients, "app=invalid-node-selector-demo")

    finding = scheduling_service.analyze_pod(LAB_NAMESPACE, pod_name)

    assert finding.workload_kind == "Deployment"
    assert finding.workload_name == "invalid-node-selector-demo"


def test_full_analyze_pass_never_issues_a_non_get_http_request(live_cluster_clients, scheduling_service):
    pod_name = _pod_name(live_cluster_clients, "app=pending-resources-demo")
    pool_manager = live_cluster_clients.core.api_client.rest_client.pool_manager
    original_request = pool_manager.request
    methods_seen: set[str] = set()

    def spy(method, url, *args, **kwargs):
        methods_seen.add(method)
        return original_request(method, url, *args, **kwargs)

    with patch.object(pool_manager, "request", side_effect=spy):
        scheduling_service.analyze_pod(LAB_NAMESPACE, pod_name)

    assert methods_seen, "expected at least one HTTP call to have been made"
    assert methods_seen <= {"GET"}, f"a full analyze_pod() pass issued non-GET HTTP method(s): {methods_seen}"

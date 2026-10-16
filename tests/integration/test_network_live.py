"""Live kind-cluster scenarios for the network troubleshooting engine
(docs/NETWORK_DIAGNOSTICS.md Section 10's "Live" tier). Real Services,
real Pods, real EndpointSlices and real coredns -- never mocked -- confirms
the engine's independently-computed selector/port matching reaches the
right conclusion against actual cluster state, and that a full analyze
pass never issues a mutating call against the cluster.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kubesentinel.models.network import NetworkFindingClassification
from kubesentinel.network.service import NetworkService
from kubesentinel.persistence.network_repo import SQLiteNetworkRepository
from tests.conftest import LAB_NAMESPACE

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")


@pytest.fixture
def network_service(live_cluster_clients):
    with tempfile.TemporaryDirectory() as d:
        repo = SQLiteNetworkRepository(str(Path(d) / "test.db"))
        try:
            yield NetworkService(live_cluster_clients, repo)
        finally:
            repo.close()


def _pod_name(live_cluster_clients, label_selector: str) -> str:
    pods = live_cluster_clients.core.list_namespaced_pod(namespace=LAB_NAMESPACE, label_selector=label_selector)
    if not pods.items:
        pytest.skip(f"no pod matching '{label_selector}' found -- run the matching lab scenario first")
    return pods.items[0].metadata.name


def test_port_mismatch_scenario_is_configuration_error(live_cluster_clients, network_service):
    pod_name = _pod_name(live_cluster_clients, "app=port-mismatch-demo")

    finding = network_service.analyze_pod(LAB_NAMESPACE, pod_name)

    assert finding.classification == NetworkFindingClassification.CONFIGURATION_ERROR
    target = next(r for r in finding.service_results if r.service_name == "port-mismatch-demo")
    assert pod_name in target.ready_pod_names
    bad_port = next(p for p in target.port_results if p.target_port == "web")
    assert bad_port.matched is False


def test_port_mismatch_scenario_resolves_the_owning_deployment(live_cluster_clients, network_service):
    pod_name = _pod_name(live_cluster_clients, "app=port-mismatch-demo")

    finding = network_service.analyze_pod(LAB_NAMESPACE, pod_name)

    assert finding.workload_kind == "Deployment"
    assert finding.workload_name == "port-mismatch-demo"


def test_pod_selected_by_no_service_is_inconclusive(live_cluster_clients, network_service):
    pod_name = _pod_name(live_cluster_clients, "app=remediation-test")

    finding = network_service.analyze_pod(LAB_NAMESPACE, pod_name)

    assert finding.classification == NetworkFindingClassification.INCONCLUSIVE


def test_dns_health_reads_the_real_coredns_and_kube_dns(live_cluster_clients, network_service):
    pod_name = _pod_name(live_cluster_clients, "app=remediation-test")

    finding = network_service.analyze_pod(LAB_NAMESPACE, pod_name)

    assert finding.dns_health.healthy is True
    assert finding.dns_health.coredns_ready_replicas is not None and finding.dns_health.coredns_ready_replicas > 0
    assert finding.dns_health.kube_dns_endpoint_count is not None and finding.dns_health.kube_dns_endpoint_count > 0


def test_full_analyze_pass_never_issues_a_non_get_http_request(live_cluster_clients, network_service):
    pod_name = _pod_name(live_cluster_clients, "app=port-mismatch-demo")
    pool_manager = live_cluster_clients.core.api_client.rest_client.pool_manager
    original_request = pool_manager.request
    methods_seen: set[str] = set()

    def spy(method, url, *args, **kwargs):
        methods_seen.add(method)
        return original_request(method, url, *args, **kwargs)

    with patch.object(pool_manager, "request", side_effect=spy):
        network_service.analyze_pod(LAB_NAMESPACE, pod_name)

    assert methods_seen, "expected at least one HTTP call to have been made"
    assert methods_seen <= {"GET"}, f"a full analyze_pod() pass issued non-GET HTTP method(s): {methods_seen}"

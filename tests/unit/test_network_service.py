import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client import V1Deployment, V1DeploymentSpec, V1DeploymentStatus, V1ObjectMeta, V1PodCondition, V1Service
from kubernetes.client.rest import ApiException

from kubesentinel.metrics.registry import network_analysis_total
from kubesentinel.models.network import NetworkFindingClassification
from kubesentinel.network.service import FindingNotFoundError, NetworkService, PodNotFoundError
from kubesentinel.persistence.network_repo import SQLiteNetworkRepository
from tests.unit.factories import make_pod, make_service

NAMESPACE = "kubesentinel-lab"


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteNetworkRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _items(objs):
    return SimpleNamespace(items=objs)


class _FakeRawResponse:
    def __init__(self, items: list[dict]):
        self.data = json.dumps({"items": items}).encode()


def _target_pod(name="target", ready=True):
    pod = make_pod(name=name, namespace=NAMESPACE, conditions=[V1PodCondition(type="Ready", status="True" if ready else "False")])
    pod.metadata.labels = {"app": "target"}
    return pod


def _endpoint(ready=True):
    return {"conditions": {"ready": ready}}


def _endpointslice(service_name, ready_count=1):
    return {
        "metadata": {"labels": {"kubernetes.io/service-name": service_name}},
        "endpoints": [_endpoint(ready=True) for _ in range(ready_count)],
    }


def _clients(pod=None, pod_exists=True, services=(), dns_healthy=True):
    clients = MagicMock()
    if pod_exists:
        clients.core.read_namespaced_pod.return_value = pod or _target_pod()
    else:
        clients.core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")
    clients.apps.list_namespaced_replica_set.return_value = _items([])
    clients.core.list_namespaced_service.return_value = _items(list(services))
    clients.core.list_namespaced_pod.return_value = _items([pod or _target_pod()])
    clients.networking.list_namespaced_network_policy.return_value = _items([])

    def _list_endpoint_slices(*args, **kwargs):
        if kwargs.get("namespace") == "kube-system":
            return _FakeRawResponse([_endpointslice("kube-dns", ready_count=1)] if dns_healthy else [])
        return _FakeRawResponse([_endpointslice(svc.metadata.name, ready_count=1) for svc in services])

    clients.discovery.list_namespaced_endpoint_slice.side_effect = _list_endpoint_slices

    ready_replicas = 2 if dns_healthy else 0
    clients.apps.read_namespaced_deployment.return_value = V1Deployment(
        metadata=V1ObjectMeta(name="coredns", namespace="kube-system"),
        spec=V1DeploymentSpec(replicas=2, selector={}, template={}),
        status=V1DeploymentStatus(ready_replicas=ready_replicas),
    )
    clients.core.read_namespaced_service.return_value = V1Service(metadata=V1ObjectMeta(name="kube-dns"))
    return clients


class TestAnalyzePod:
    def test_persists_and_returns_finding(self, repo):
        svc = make_service(name="svc", selector={"app": "target"})
        clients = _clients(services=[svc])
        service = NetworkService(clients, repo)

        finding = service.analyze_pod(NAMESPACE, "target")

        assert finding.target_pod_name == "target"
        assert finding.classification == NetworkFindingClassification.HEALTHY
        assert repo.get_finding(finding.finding_id) is not None

    def test_missing_pod_raises_pod_not_found(self, repo):
        clients = _clients(pod_exists=False)
        service = NetworkService(clients, repo)

        with pytest.raises(PodNotFoundError):
            service.analyze_pod(NAMESPACE, "does-not-exist")

    def test_dns_unhealthy_classification(self, repo):
        clients = _clients(dns_healthy=False)
        service = NetworkService(clients, repo)

        finding = service.analyze_pod(NAMESPACE, "target")

        assert finding.classification == NetworkFindingClassification.DNS_UNHEALTHY


class TestGetAndList:
    def test_get_missing_finding_raises(self, repo):
        clients = _clients()
        service = NetworkService(clients, repo)

        with pytest.raises(FindingNotFoundError):
            service.get_finding("does-not-exist")

    def test_list_findings_after_analyze(self, repo):
        clients = _clients()
        service = NetworkService(clients, repo)

        service.analyze_pod(NAMESPACE, "target")

        assert len(service.list_findings(namespace=NAMESPACE)) == 1


class TestIncidentLinking:
    def test_no_incident_repo_wired_up_is_a_safe_no_op(self, repo):
        clients = _clients()
        service = NetworkService(clients, repo)

        finding = service.analyze_pod(NAMESPACE, "target")

        assert finding.related_incident_ids == []

    def test_open_incident_on_pod_uid_is_linked(self, repo):
        pod = _target_pod()  # default uid is "uid-1234" (tests/unit/factories.py)
        clients = _clients(pod=pod)
        incident_repo = MagicMock()
        incident_repo.find_open_by_resource_uid.side_effect = lambda uid: (
            SimpleNamespace(incident_id="incident-1") if uid == "uid-1234" else None
        )
        service = NetworkService(clients, repo, incident_repo=incident_repo)

        finding = service.analyze_pod(NAMESPACE, "target")

        assert finding.related_incident_ids == ["incident-1"]


class TestMetrics:
    def _reset(self):
        for label in ("healthy", "configuration_error", "missing_or_unready_endpoints", "dns_unhealthy", "policy_may_restrict", "inconclusive", "failed"):
            network_analysis_total.labels(result=label)._value.set(0)

    def test_healthy_result_increments_healthy_counter(self, repo):
        self._reset()
        svc = make_service(name="svc", selector={"app": "target"})
        clients = _clients(services=[svc])
        service = NetworkService(clients, repo)

        finding = service.analyze_pod(NAMESPACE, "target")

        assert finding.classification == NetworkFindingClassification.HEALTHY
        assert network_analysis_total.labels(result="healthy")._value.get() == 1

    def test_failure_increments_failed_counter_and_reraises(self, repo):
        self._reset()
        clients = _clients(pod_exists=False)
        service = NetworkService(clients, repo)

        with pytest.raises(PodNotFoundError):
            service.analyze_pod(NAMESPACE, "does-not-exist")

        assert network_analysis_total.labels(result="failed")._value.get() == 1

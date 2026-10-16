"""API tests for the network troubleshooting engine's routes -- same
"bypass lifespan, set state directly" pattern as test_scheduling_api.py."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from kubernetes.client import V1Deployment, V1DeploymentSpec, V1DeploymentStatus, V1ObjectMeta, V1PodCondition, V1Service
from kubernetes.client.rest import ApiException

from kubesentinel.api import app as app_module
from kubesentinel.network.service import NetworkService
from kubesentinel.persistence.network_repo import SQLiteNetworkRepository
from tests.unit.factories import make_pod, make_service

client = TestClient(app_module.app)
NAMESPACE = "kubesentinel-lab"


def _items(objs):
    return SimpleNamespace(items=objs)


class _FakeRawResponse:
    def __init__(self, items: list[dict]):
        self.data = json.dumps({"items": items}).encode()


def _target_pod():
    pod = make_pod(name="target", namespace=NAMESPACE, conditions=[V1PodCondition(type="Ready", status="True")])
    pod.metadata.labels = {"app": "target"}
    return pod


def _endpointslice(service_name, ready_count=1):
    return {
        "metadata": {"labels": {"kubernetes.io/service-name": service_name}},
        "endpoints": [{"conditions": {"ready": True}} for _ in range(ready_count)],
    }


def _clients(services=()):
    clients = MagicMock()
    clients.core.read_namespaced_pod.return_value = _target_pod()
    clients.apps.list_namespaced_replica_set.return_value = _items([])
    clients.core.list_namespaced_service.return_value = _items(list(services))
    clients.core.list_namespaced_pod.return_value = _items([_target_pod()])
    clients.networking.list_namespaced_network_policy.return_value = _items([])

    def _list_endpoint_slices(*args, **kwargs):
        if kwargs.get("namespace") == "kube-system":
            return _FakeRawResponse([_endpointslice("kube-dns", ready_count=1)])
        return _FakeRawResponse([_endpointslice(svc.metadata.name, ready_count=1) for svc in services])

    clients.discovery.list_namespaced_endpoint_slice.side_effect = _list_endpoint_slices
    clients.apps.read_namespaced_deployment.return_value = V1Deployment(
        metadata=V1ObjectMeta(name="coredns", namespace="kube-system"),
        spec=V1DeploymentSpec(replicas=2, selector={}, template={}),
        status=V1DeploymentStatus(ready_replicas=2),
    )
    clients.core.read_namespaced_service.return_value = V1Service(metadata=V1ObjectMeta(name="kube-dns"))
    return clients


@pytest.fixture
def api_state():
    with tempfile.TemporaryDirectory() as d:
        repo = SQLiteNetworkRepository(str(Path(d) / "network.db"))

        svc = make_service(name="svc", selector={"app": "target"})
        clients = _clients(services=[svc])

        app_module.state.network = NetworkService(clients, repo)

        yield repo
        repo.close()


def test_analyze_pod(api_state):
    resp = client.post("/network/analyze", json={"namespace": NAMESPACE, "pod_name": "target"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_pod_name"] == "target"
    assert body["classification"] == "HEALTHY"


def test_analyze_missing_pod_is_404(api_state):
    app_module.state.network._clients.core.read_namespaced_pod.side_effect = ApiException(  # type: ignore[union-attr]
        status=404, reason="Not Found"
    )
    resp = client.post("/network/analyze", json={"namespace": NAMESPACE, "pod_name": "does-not-exist"})
    assert resp.status_code == 404


def test_network_service_unavailable_is_503():
    app_module.state.network = None
    resp = client.post("/network/analyze", json={"namespace": NAMESPACE, "pod_name": "x"})
    assert resp.status_code == 503


def test_list_findings_after_analyze(api_state):
    client.post("/network/analyze", json={"namespace": NAMESPACE, "pod_name": "target"})
    resp = client.get("/network/findings")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_findings_filters_by_namespace(api_state):
    client.post("/network/analyze", json={"namespace": NAMESPACE, "pod_name": "target"})
    resp = client.get("/network/findings", params={"namespace": "some-other-namespace"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_findings_invalid_classification_is_422(api_state):
    resp = client.get("/network/findings", params={"classification": "not-a-real-value"})
    assert resp.status_code == 422


def test_get_finding_by_id(api_state):
    client.post("/network/analyze", json={"namespace": NAMESPACE, "pod_name": "target"})
    finding_id = client.get("/network/findings").json()[0]["finding_id"]

    resp = client.get(f"/network/findings/{finding_id}")

    assert resp.status_code == 200
    assert resp.json()["finding_id"] == finding_id


def test_get_finding_not_found_is_404(api_state):
    resp = client.get("/network/findings/does-not-exist")
    assert resp.status_code == 404

"""API tests for the scheduling advisor's routes -- same "bypass lifespan,
set state directly" pattern as test_api.py/test_optimization_api.py."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from kubernetes.client import V1Container, V1PodSpec, V1PodStatus, V1ResourceRequirements
from kubernetes.client.rest import ApiException

from kubesentinel.api import app as app_module
from kubesentinel.persistence.scheduling_repo import SQLiteSchedulingRepository
from kubesentinel.scheduling.service import SchedulingService
from tests.unit.factories import make_node, make_pod

client = TestClient(app_module.app)
NAMESPACE = "kubesentinel-lab"


def _items(objs):
    return SimpleNamespace(items=objs)


def _pending_pod():
    pod = make_pod(name="pending-pod", namespace=NAMESPACE, phase="Pending")
    pod.spec = V1PodSpec(
        containers=[V1Container(name="app", resources=V1ResourceRequirements(requests={"cpu": "100m", "memory": "64Mi"}))]
    )
    pod.status = V1PodStatus(phase="Pending", conditions=[])
    return pod


@pytest.fixture
def api_state():
    with tempfile.TemporaryDirectory() as d:
        repo = SQLiteSchedulingRepository(str(Path(d) / "sched.db"))

        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = MagicMock()
        clients.core.read_namespaced_pod.return_value = _pending_pod()
        clients.core.list_node.return_value = _items([node])
        clients.core.list_pod_for_all_namespaces.return_value = _items([])
        clients.apps.list_namespaced_replica_set.return_value = _items([])
        clients.core.list_namespaced_resource_quota.return_value = _items([])

        app_module.state.scheduling = SchedulingService(clients, repo)

        yield repo
        repo.close()


def test_analyze_pod(api_state):
    resp = client.post("/scheduling/analyze", json={"namespace": NAMESPACE, "pod_name": "pending-pod"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["any_node_schedulable"] is True


def test_analyze_missing_pod_is_404(api_state):
    app_module.state.scheduling._clients.core.read_namespaced_pod.side_effect = ApiException(  # type: ignore[union-attr]
        status=404, reason="Not Found"
    )
    resp = client.post("/scheduling/analyze", json={"namespace": NAMESPACE, "pod_name": "does-not-exist"})
    assert resp.status_code == 404


def test_scheduling_service_unavailable_is_503():
    app_module.state.scheduling = None
    resp = client.post("/scheduling/analyze", json={"namespace": NAMESPACE, "pod_name": "x"})
    assert resp.status_code == 503


def test_simulate_hypothetical_spec(api_state):
    resp = client.post(
        "/scheduling/simulate",
        json={
            "namespace": NAMESPACE,
            "pod_spec": {"containers": [{"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pod_name"] is None
    assert body["any_node_schedulable"] is True


def test_simulate_invalid_spec_is_422(api_state):
    resp = client.post(
        "/scheduling/simulate",
        json={"namespace": NAMESPACE, "pod_spec": {"containers": "this-should-be-a-list-not-a-string"}},
    )
    assert resp.status_code == 422


def test_list_findings_after_analyze(api_state):
    client.post("/scheduling/analyze", json={"namespace": NAMESPACE, "pod_name": "pending-pod"})
    resp = client.get("/scheduling/findings")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_findings_filters_by_namespace(api_state):
    client.post("/scheduling/analyze", json={"namespace": NAMESPACE, "pod_name": "pending-pod"})
    resp = client.get("/scheduling/findings", params={"namespace": "some-other-namespace"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_finding_by_id(api_state):
    client.post("/scheduling/analyze", json={"namespace": NAMESPACE, "pod_name": "pending-pod"})
    finding_id = client.get("/scheduling/findings").json()[0]["finding_id"]

    resp = client.get(f"/scheduling/findings/{finding_id}")

    assert resp.status_code == 200
    assert resp.json()["finding_id"] == finding_id


def test_get_finding_not_found_is_404(api_state):
    resp = client.get("/scheduling/findings/does-not-exist")
    assert resp.status_code == 404

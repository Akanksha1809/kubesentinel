"""API tests for the security & configuration auditor's routes -- same
"bypass lifespan, set state directly" pattern as test_network_api.py.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from kubernetes.client import V1Container, V1PodSpec, V1SecurityContext

from kubesentinel.api import app as app_module
from kubesentinel.config import Settings
from kubesentinel.persistence.security_repo import SQLiteSecurityRepository
from kubesentinel.security.service import SecurityService
from tests.unit.factories import make_pod

client = TestClient(app_module.app)
NAMESPACE = "kubesentinel-lab"


def _items(objs):
    return SimpleNamespace(items=objs)


def _privileged_pod():
    pod = make_pod(name="demo", namespace=NAMESPACE, owner=None)
    pod.spec = V1PodSpec(containers=[V1Container(name="app", security_context=V1SecurityContext(privileged=True))])
    return pod


def _clients(pods=()):
    clients = MagicMock()
    clients.core.list_namespaced_pod.return_value = _items(list(pods))
    clients.apps.list_namespaced_replica_set.return_value = _items([])
    clients.apps.list_namespaced_deployment.return_value = _items([])
    clients.apps.list_namespaced_stateful_set.return_value = _items([])
    clients.rbac_authorization.list_namespaced_role_binding.return_value = _items([])
    clients.rbac_authorization.list_namespaced_role.return_value = _items([])
    clients.rbac_authorization.list_cluster_role_binding.return_value = _items([])
    clients.rbac_authorization.list_cluster_role.return_value = _items([])
    clients.networking.list_namespaced_network_policy.return_value = _items([])
    clients.policy.list_namespaced_pod_disruption_budget.return_value = _items([])
    return clients


@pytest.fixture
def api_state():
    with tempfile.TemporaryDirectory() as d:
        repo = SQLiteSecurityRepository(str(Path(d) / "security.db"))

        clients = _clients(pods=[_privileged_pod()])
        app_module.state.security = SecurityService(clients, repo, Settings())

        yield repo
        repo.close()


def test_audit(api_state):
    resp = client.post("/security/audit", json={"namespaces": [NAMESPACE]})
    assert resp.status_code == 200
    body = resp.json()
    assert any(f["rule_id"] == "PRIVILEGED_CONTAINER" for f in body["findings"])


def test_security_service_unavailable_is_503():
    app_module.state.security = None
    resp = client.post("/security/audit", json={"namespaces": [NAMESPACE]})
    assert resp.status_code == 503


def test_list_findings_after_audit(api_state):
    client.post("/security/audit", json={"namespaces": [NAMESPACE]})
    resp = client.get("/security/findings")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_list_findings_filters_by_namespace(api_state):
    client.post("/security/audit", json={"namespaces": [NAMESPACE]})
    resp = client.get("/security/findings", params={"namespace": "some-other-namespace"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_findings_invalid_severity_is_422(api_state):
    resp = client.get("/security/findings", params={"severity": "not-a-real-value"})
    assert resp.status_code == 422


def test_list_findings_filters_by_rule_id(api_state):
    client.post("/security/audit", json={"namespaces": [NAMESPACE]})
    resp = client.get("/security/findings", params={"rule_id": "PRIVILEGED_CONTAINER"})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_get_finding_by_id(api_state):
    client.post("/security/audit", json={"namespaces": [NAMESPACE]})
    finding_id = client.get("/security/findings").json()[0]["finding_id"]

    resp = client.get(f"/security/findings/{finding_id}")

    assert resp.status_code == 200
    assert resp.json()["finding_id"] == finding_id


def test_get_finding_not_found_is_404(api_state):
    resp = client.get("/security/findings/does-not-exist")
    assert resp.status_code == 404

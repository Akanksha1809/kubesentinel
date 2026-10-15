"""API tests for the optimization engine's routes -- same "bypass lifespan,
set state directly" pattern as test_api.py."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from kubernetes.client import V1Container, V1PodCondition, V1PodSpec
from kubernetes.client.rest import ApiException

from kubesentinel.api import app as app_module
from kubesentinel.config import Settings
from kubesentinel.optimization.service import OptimizationService
from kubesentinel.persistence.optimization_repo import SQLiteOptimizationRepository
from tests.unit.factories import make_pod

client = TestClient(app_module.app)
NAMESPACE = "kubesentinel-lab"


def _items(objs):
    return SimpleNamespace(items=objs)


def _standalone_pod_missing_resources():
    pod = make_pod(name="standalone", uid="pod-uid-1", owner=None, conditions=[V1PodCondition(type="Ready", status="True")])
    pod.spec = V1PodSpec(containers=[V1Container(name="app", resources=None)])
    return pod


@pytest.fixture
def api_state():
    with tempfile.TemporaryDirectory() as d:
        settings = Settings(
            api_token="test-token",
            db_path=str(Path(d) / "test.db"),
            optimization_recommendation_ttl_seconds=3600.0,
        )
        repo = SQLiteOptimizationRepository(str(Path(d) / "opt.db"))

        clients = MagicMock()
        clients.core.list_namespaced_pod.return_value = _items([_standalone_pod_missing_resources()])
        clients.core.list_pod_for_all_namespaces.return_value = _items([_standalone_pod_missing_resources()])
        clients.apps.list_namespaced_replica_set.return_value = _items([])
        clients.apps.list_replica_set_for_all_namespaces.return_value = _items([])
        clients.apps.list_namespaced_deployment.return_value = _items([])
        clients.apps.list_deployment_for_all_namespaces.return_value = _items([])
        clients.apps.list_namespaced_stateful_set.return_value = _items([])
        clients.apps.list_stateful_set_for_all_namespaces.return_value = _items([])
        clients.apps.list_namespaced_daemon_set.return_value = _items([])
        clients.apps.list_daemon_set_for_all_namespaces.return_value = _items([])
        clients.batch.list_namespaced_job.return_value = _items([])
        clients.batch.list_job_for_all_namespaces.return_value = _items([])
        clients.custom_objects.list_namespaced_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        clients.custom_objects.list_cluster_custom_object.side_effect = ApiException(status=404, reason="Not Found")

        app_module.state.settings = settings
        app_module.state.optimization = OptimizationService(clients, repo, settings)

        yield repo
        repo.close()


def test_analyze_without_body_analyzes_all_namespaces(api_state):
    resp = client.post("/optimization/analyze")
    assert resp.status_code == 200
    body = resp.json()
    assert body["workload_count"] == 1
    assert body["metrics_available"] is False


def test_analyze_with_namespace_filter(api_state):
    resp = client.post("/optimization/analyze", json={"namespaces": [NAMESPACE]})
    assert resp.status_code == 200
    assert resp.json()["workload_count"] == 1


def test_optimization_service_unavailable_is_503():
    app_module.state.optimization = None
    resp = client.post("/optimization/analyze")
    assert resp.status_code == 503


def test_analyze_cluster_unreachable_mid_call_is_503():
    """Distinct from `test_optimization_service_unavailable_is_503`: here the
    service exists (the cluster was reachable at startup), but a specific
    call into it fails -- matches a real failure caught live during this
    milestone's own kind-cluster verification (a transient connectivity
    blip during `/analyze`), where this used to surface as a bare 500."""
    with tempfile.TemporaryDirectory() as d:
        settings = Settings(db_path=str(Path(d) / "test.db"))
        repo = SQLiteOptimizationRepository(str(Path(d) / "opt.db"))
        clients = MagicMock()
        clients.core.list_namespaced_pod.side_effect = ApiException(status=500, reason="cluster unreachable")
        clients.core.list_pod_for_all_namespaces.side_effect = ApiException(status=500, reason="cluster unreachable")
        app_module.state.optimization = OptimizationService(clients, repo, settings)

        resp = client.post("/optimization/analyze")

        assert resp.status_code == 503
        repo.close()


def test_list_workloads_after_analyze(api_state):
    client.post("/optimization/analyze", json={"namespaces": [NAMESPACE]})
    resp = client.get("/optimization/workloads")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_recommendations_after_analyze(api_state):
    client.post("/optimization/analyze", json={"namespaces": [NAMESPACE]})
    resp = client.get("/optimization/recommendations")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    assert resp.json()[0]["category"] == "DATA_QUALITY"


def test_list_recommendations_filters_by_category(api_state):
    client.post("/optimization/analyze", json={"namespaces": [NAMESPACE]})
    resp = client.get("/optimization/recommendations", params={"category": "CPU"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_recommendations_invalid_category_is_422(api_state):
    resp = client.get("/optimization/recommendations", params={"category": "NOT_A_REAL_CATEGORY"})
    assert resp.status_code == 422


def test_list_recommendations_invalid_status_is_422(api_state):
    resp = client.get("/optimization/recommendations", params={"status": "NOT_A_REAL_STATUS"})
    assert resp.status_code == 422


def test_get_recommendation_not_found_is_404(api_state):
    resp = client.get("/optimization/recommendations/does-not-exist")
    assert resp.status_code == 404


def test_get_recommendation_by_id(api_state):
    client.post("/optimization/analyze", json={"namespaces": [NAMESPACE]})
    rec_id = client.get("/optimization/recommendations").json()[0]["recommendation_id"]

    resp = client.get(f"/optimization/recommendations/{rec_id}")

    assert resp.status_code == 200
    assert resp.json()["recommendation_id"] == rec_id


def test_summary_reflects_analysis(api_state):
    client.post("/optimization/analyze", json={"namespaces": [NAMESPACE]})
    resp = client.get("/optimization/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_recommendations"] >= 1
    assert body["latest_analysis"]["metrics_available"] is False


def test_review_without_authorization_header_is_401(api_state):
    # This fixture *does* configure a token (to test the success path below),
    # so a missing header is a wrong-credential case (401) -- the "disabled
    # entirely" (503) behavior for an *unset* token is already covered
    # exhaustively for the shared `require_api_token` dependency in
    # test_api.py; re-asserting it here would just be redundant.
    client.post("/optimization/analyze", json={"namespaces": [NAMESPACE]})
    rec_id = client.get("/optimization/recommendations").json()[0]["recommendation_id"]

    resp = client.post(
        f"/optimization/recommendations/{rec_id}/review",
        json={"decision": "APPROVED", "reviewed_by": "akanksha"},
    )

    assert resp.status_code == 401


def test_review_with_token_approves(api_state):
    client.post("/optimization/analyze", json={"namespaces": [NAMESPACE]})
    rec_id = client.get("/optimization/recommendations").json()[0]["recommendation_id"]

    resp = client.post(
        f"/optimization/recommendations/{rec_id}/review",
        json={"decision": "APPROVED", "reviewed_by": "akanksha", "notes": "looks right"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "APPROVED"
    assert resp.json()["reviewed_by"] == "akanksha"


def test_review_already_decided_is_409(api_state):
    client.post("/optimization/analyze", json={"namespaces": [NAMESPACE]})
    rec_id = client.get("/optimization/recommendations").json()[0]["recommendation_id"]
    headers = {"Authorization": "Bearer test-token"}
    client.post(f"/optimization/recommendations/{rec_id}/review", json={"decision": "REJECTED", "reviewed_by": "a"}, headers=headers)

    resp = client.post(f"/optimization/recommendations/{rec_id}/review", json={"decision": "APPROVED", "reviewed_by": "a"}, headers=headers)

    assert resp.status_code == 409


def test_review_missing_recommendation_is_404(api_state):
    resp = client.post(
        "/optimization/recommendations/does-not-exist/review",
        json={"decision": "APPROVED", "reviewed_by": "akanksha"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 404

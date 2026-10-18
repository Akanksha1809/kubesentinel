"""API tests. Deliberately bypasses `lifespan` (which connects to a real cluster)
by setting module-level `state` attributes directly before issuing requests --
`TestClient(app)` without a `with` block never triggers lifespan startup/shutdown,
so this is the whole app's real routing/validation/auth logic under test, just
without a live cluster behind it."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from kubesentinel.api import app as app_module
from kubesentinel.config import PolicyMode, Settings
from kubesentinel.incidents.manager import IncidentManager
from kubesentinel.models.incident import Incident, IncidentSeverity, IncidentStatus, ResourceRef
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from kubesentinel.policy.engine import PolicyEngine
from kubesentinel.remediation.executor import RemediationExecutor
from kubesentinel.remediation.service import RemediationService
from kubesentinel.verification.verifier import RecoveryVerifier
from tests.unit.factories import make_deployment

client = TestClient(app_module.app)


@pytest.fixture
def api_state():
    with tempfile.TemporaryDirectory() as d:
        settings = Settings(
            api_token="test-token",
            policy_mode=PolicyMode.APPROVAL_REQUIRED,
            action_allowed_namespaces="kubesentinel-lab",
            db_path=str(Path(d) / "test.db"),
            verification_deadline_seconds=0.05,
            verification_poll_interval_seconds=0.01,
        )
        repo = SQLiteIncidentRepository(settings.db_path)
        apps = MagicMock()
        apps.read_namespaced_deployment.return_value = make_deployment(
            uid="deploy-uid-1234", desired_replicas=2, ready_replicas=2, available_replicas=2, updated_replicas=2
        )
        executor = RemediationExecutor(apps)
        verifier = RecoveryVerifier(apps, deadline_seconds=0.05, poll_interval_seconds=0.01)

        app_module.state.settings = settings
        app_module.state.repo = repo
        app_module.state.manager = IncidentManager(repo)
        app_module.state.observer = MagicMock()
        app_module.state.remediation = RemediationService(PolicyEngine(settings), executor, verifier, repo, settings)
        app_module.state.ai_client = None  # explicit per-test reset -- tests mutate this directly

        yield repo, apps
        repo.close()


def _save_incident(repo, incident_id: str = "incident-1") -> Incident:
    now = datetime.now(timezone.utc)
    incident = Incident(
        incident_id=incident_id,
        resource=ResourceRef(
            namespace="kubesentinel-lab", kind="Deployment", name="test-deployment", uid="deploy-uid-1234"
        ),
        category="CRASH_LOOP_BACKOFF",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.DIAGNOSED,
        first_observed_at=now,
        last_observed_at=now,
    )
    repo.save(incident)
    return incident


def test_health_and_ready(api_state):
    assert client.get("/health").json() == {"status": "ok"}
    resp = client.get("/ready").json()
    assert resp["status"] == "ready"


def test_get_incident_not_found_returns_404(api_state):
    resp = client.get("/incidents/does-not-exist")
    assert resp.status_code == 404


def test_list_and_get_incident(api_state):
    repo, _ = api_state
    _save_incident(repo)

    listing = client.get("/incidents").json()
    assert len(listing) == 1
    assert listing[0]["incident_id"] == "incident-1"

    detail = client.get("/incidents/incident-1").json()
    assert detail["category"] == "CRASH_LOOP_BACKOFF"


def test_plan_without_token_is_rejected_as_disabled(api_state):
    app_module.state.settings.api_token = ""  # simulate no token configured
    resp = client.post(
        "/incidents/incident-1/actions/plan",
        json={"action_type": "RESTART_DEPLOYMENT", "rationale": "test"},
    )
    assert resp.status_code == 503


def test_plan_with_wrong_token_is_401(api_state):
    repo, _ = api_state
    _save_incident(repo)
    resp = client.post(
        "/incidents/incident-1/actions/plan",
        json={"action_type": "RESTART_DEPLOYMENT", "rationale": "test"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_plan_with_correct_token_creates_pending_action(api_state):
    repo, _ = api_state
    _save_incident(repo)
    resp = client.post(
        "/incidents/incident-1/actions/plan",
        json={"action_type": "RESTART_DEPLOYMENT", "rationale": "test"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PENDING_APPROVAL"


def test_plan_dry_run_never_persists(api_state):
    repo, apps = api_state
    _save_incident(repo)
    resp = client.post(
        "/incidents/incident-1/actions/plan",
        json={"action_type": "RESTART_DEPLOYMENT", "rationale": "test", "dry_run": True},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    assert resp.json()["outcome"] == "REQUIRES_APPROVAL"
    assert repo.get("incident-1").actions == []
    apps.patch_namespaced_deployment.assert_not_called()


def test_plan_for_missing_incident_is_404(api_state):
    resp = client.post(
        "/incidents/does-not-exist/actions/plan",
        json={"action_type": "RESTART_DEPLOYMENT", "rationale": "test"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 404


def test_full_plan_approve_execute_flow(api_state):
    repo, apps = api_state
    _save_incident(repo)
    headers = {"Authorization": "Bearer test-token"}

    planned = client.post(
        "/incidents/incident-1/actions/plan",
        json={"action_type": "RESTART_DEPLOYMENT", "rationale": "test"},
        headers=headers,
    ).json()
    action_id = planned["action_id"]
    assert planned["status"] == "PENDING_APPROVAL"

    approved = client.post(
        f"/incidents/incident-1/actions/{action_id}/approve",
        json={"approved_by": "akanksha"},
        headers=headers,
    ).json()
    assert approved["status"] == "APPROVED"
    apps.patch_namespaced_deployment.assert_not_called()  # approve alone must not execute

    executed = client.post(
        f"/incidents/incident-1/actions/{action_id}/execute",
        headers=headers,
    ).json()
    assert executed["status"] == "VERIFIED_RECOVERED"
    apps.patch_namespaced_deployment.assert_called_once()


def test_approve_unknown_action_is_404(api_state):
    repo, _ = api_state
    _save_incident(repo)
    resp = client.post(
        "/incidents/incident-1/actions/does-not-exist/approve",
        json={"approved_by": "akanksha"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 404


def test_execute_before_approve_is_409(api_state):
    repo, _ = api_state
    _save_incident(repo)
    headers = {"Authorization": "Bearer test-token"}
    planned = client.post(
        "/incidents/incident-1/actions/plan",
        json={"action_type": "RESTART_DEPLOYMENT", "rationale": "test"},
        headers=headers,
    ).json()

    resp = client.post(f"/incidents/incident-1/actions/{planned['action_id']}/execute", headers=headers)
    assert resp.status_code == 409


def test_reject_sets_rejected_status(api_state):
    repo, _ = api_state
    _save_incident(repo)
    headers = {"Authorization": "Bearer test-token"}
    planned = client.post(
        "/incidents/incident-1/actions/plan",
        json={"action_type": "RESTART_DEPLOYMENT", "rationale": "test"},
        headers=headers,
    ).json()

    resp = client.post(
        f"/incidents/incident-1/actions/{planned['action_id']}/reject",
        json={"rejected_by": "akanksha", "reason": "not needed"},
        headers=headers,
    )
    assert resp.status_code == 200


# -- Standalone action routes (no owning incident at all) -- same sibling
# shape as the /incidents/{id}/actions/... routes above, reached via
# /actions/{action_id}/... instead. docs/RESOURCE_OPTIMIZATION_DESIGN.md
# Section 14.5.


def _propose_standalone(target_uid: str = "deploy-uid-1234"):
    from kubesentinel.models.action import ActionTarget, ActionType

    return app_module.state.remediation.propose_standalone(
        target=ActionTarget(
            namespace="kubesentinel-lab", resource_type="Deployment", resource_name="test-deployment",
            resource_uid=target_uid,
        ),
        action_type=ActionType.ADJUST_RESOURCES,
        rationale="right-size",
        category="RESOURCE_OPTIMIZATION",
        parameters={"container_name": "app", "requests": {"cpu": "500m"}, "limits": {}},
    )


def test_full_standalone_approve_execute_flow(api_state):
    _repo, apps = api_state
    action = _propose_standalone()
    assert action.status.value == "PENDING_APPROVAL"
    headers = {"Authorization": "Bearer test-token"}

    get_resp = client.get(f"/actions/{action.action_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["incident_id"] is None

    approved = client.post(
        f"/actions/{action.action_id}/approve", json={"approved_by": "akanksha"}, headers=headers
    ).json()
    assert approved["status"] == "APPROVED"
    apps.patch_namespaced_deployment.assert_not_called()

    executed = client.post(f"/actions/{action.action_id}/execute", headers=headers).json()
    assert executed["status"] == "VERIFIED_RECOVERED"
    apps.patch_namespaced_deployment.assert_called_once()


def test_standalone_get_unknown_action_is_404(api_state):
    resp = client.get("/actions/does-not-exist")
    assert resp.status_code == 404


def test_standalone_approve_unknown_action_is_404(api_state):
    resp = client.post(
        "/actions/does-not-exist/approve", json={"approved_by": "akanksha"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 404


def test_standalone_execute_before_approve_is_409(api_state):
    action = _propose_standalone()
    resp = client.post(f"/actions/{action.action_id}/execute", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 409


def test_standalone_reject_sets_rejected_status(api_state):
    action = _propose_standalone()
    resp = client.post(
        f"/actions/{action.action_id}/reject",
        json={"rejected_by": "akanksha", "reason": "not needed"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"


def test_standalone_approve_without_token_is_401(api_state):
    action = _propose_standalone()
    resp = client.post(f"/actions/{action.action_id}/approve", json={"approved_by": "akanksha"})
    assert resp.status_code == 401


def test_metrics_endpoint_serves_prometheus_text(api_state):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "kubesentinel_" in resp.text


def test_ai_summary_disabled_by_default_is_503(api_state):
    repo, _ = api_state
    _save_incident(repo)
    assert app_module.state.ai_client is None  # AppState's own default

    resp = client.get("/incidents/incident-1/ai-summary")

    assert resp.status_code == 503


def test_ai_summary_for_missing_incident_is_404_even_when_enabled(api_state):
    app_module.state.ai_client = MagicMock()
    resp = client.get("/incidents/does-not-exist/ai-summary")
    assert resp.status_code == 404


def test_ai_summary_returns_text_when_enabled(api_state):
    repo, _ = api_state
    _save_incident(repo)
    fake_client = MagicMock()
    fake_client.generate.return_value = "the deployment keeps crashing"
    app_module.state.ai_client = fake_client

    resp = client.get("/incidents/incident-1/ai-summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "the deployment keeps crashing"
    assert body["incident_id"] == "incident-1"
    assert "advisory" in body["disclaimer"].lower()
    fake_client.generate.assert_called_once()


def test_ai_summary_backend_failure_is_502(api_state):
    from kubesentinel.ai.client import AiClientError

    repo, _ = api_state
    _save_incident(repo)
    fake_client = MagicMock()
    fake_client.generate.side_effect = AiClientError("ollama unreachable")
    app_module.state.ai_client = fake_client

    resp = client.get("/incidents/incident-1/ai-summary")

    assert resp.status_code == 502
    assert "unavailable" in resp.json()["detail"].lower()

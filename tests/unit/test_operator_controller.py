"""Unit tests for RemediationPolicyController.reconcile() -- see
docs/OPERATOR_DESIGN.md §5. Builds real IncidentManager-shaped Incident/
RemediationAction objects and a real SQLiteIncidentRepository/
PolicyEngine/RemediationService underneath (the same construction
cli/main.py's own `operator-run` command uses), with only the Kubernetes
API surface (`ClusterClients`) mocked -- the same split
`test_remediation_service.py` already established for testing this exact
policy->executor->verifier chain without a real cluster.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from kubesentinel.config import PolicyMode, Settings
from kubesentinel.models.action import ActionStatus, ActionTarget, ActionType, RemediationAction
from kubesentinel.models.diagnosis import Confidence, DiagnosisResult, Hypothesis, ReasonCode
from kubesentinel.models.incident import Incident, IncidentSeverity, IncidentStatus, ResourceRef
from kubesentinel.operator.controller import RemediationPolicyController
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from tests.unit.factories import make_deployment

NOW = datetime.now(timezone.utc)


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteIncidentRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _base_settings() -> Settings:
    return Settings(
        policy_mode=PolicyMode.OBSERVE,  # overridden per-CR by the controller
        action_allowed_namespaces="kubesentinel-lab",
        verification_deadline_seconds=0.05,
        verification_poll_interval_seconds=0.01,
    )


def _clients(desired_replicas: int = 3) -> MagicMock:
    clients = MagicMock()
    clients.apps.read_namespaced_deployment.return_value = make_deployment(
        uid="deploy-uid-1234",
        desired_replicas=desired_replicas,
        ready_replicas=desired_replicas,
        available_replicas=desired_replicas,
        updated_replicas=desired_replicas,
    )
    clients.apps.patch_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    return clients


def _incident_with_pending_action(
    namespace: str = "kubesentinel-lab",
    severity: IncidentSeverity = IncidentSeverity.HIGH,
    confidence: Confidence = Confidence.HIGH,
    auto_remediation_permitted: bool = True,
    action_type: ActionType = ActionType.RESTART_DEPLOYMENT,
) -> Incident:
    hypothesis = Hypothesis(
        rule_name="test_rule",
        reason_code=ReasonCode.CRASH_LOOP_BACKOFF,
        confidence=confidence,
        explanation="test",
        auto_remediation_permitted=auto_remediation_permitted,
    )
    incident = Incident(
        incident_id=str(uuid4()),
        resource=ResourceRef(namespace=namespace, kind="Deployment", name="test-deployment", uid="deploy-uid-1234"),
        category="CRASH_LOOP_BACKOFF",
        severity=severity,
        status=IncidentStatus.DIAGNOSED,
        first_observed_at=NOW,
        last_observed_at=NOW,
        diagnosis_history=[DiagnosisResult(performed_at=NOW, hypotheses=[hypothesis])],
    )
    action = RemediationAction(
        action_id=str(uuid4()),
        incident_id=incident.incident_id,
        action_type=action_type,
        target=ActionTarget(
            namespace=namespace, resource_type="Deployment", resource_name="test-deployment", resource_uid="deploy-uid-1234"
        ),
        rationale="test",
        proposed_at=NOW,
        status=ActionStatus.PENDING_APPROVAL,
    )
    incident.actions.append(action)
    return incident


def _cr(mode: str = "CONTROLLED_AUTO", namespace: str = "kubesentinel-lab", name: str = "test-policy") -> dict:
    return {
        "metadata": {"name": name, "namespace": namespace, "generation": 1},
        "spec": {"mode": mode},
    }


class TestReconcile:
    def test_observe_mode_never_approves_a_pending_action(self, repo):
        incident = _incident_with_pending_action()
        repo.save(incident)
        clients = _clients()
        controller = RemediationPolicyController(_base_settings(), clients, repo)

        controller.reconcile(_cr(mode="OBSERVE"))

        saved = repo.get(incident.incident_id)
        assert saved.actions[0].status == ActionStatus.PENDING_APPROVAL

    def test_approval_required_mode_never_auto_approves(self, repo):
        incident = _incident_with_pending_action()
        repo.save(incident)
        clients = _clients()
        controller = RemediationPolicyController(_base_settings(), clients, repo)

        controller.reconcile(_cr(mode="APPROVAL_REQUIRED"))

        saved = repo.get(incident.incident_id)
        assert saved.actions[0].status == ActionStatus.PENDING_APPROVAL

    def test_controlled_auto_approves_and_executes_an_eligible_pending_action(self, repo):
        incident = _incident_with_pending_action()
        repo.save(incident)
        clients = _clients(desired_replicas=3)
        controller = RemediationPolicyController(_base_settings(), clients, repo)

        controller.reconcile(_cr(mode="CONTROLLED_AUTO"))

        saved = repo.get(incident.incident_id)
        action = saved.actions[0]
        assert action.status != ActionStatus.PENDING_APPROVAL
        assert action.approved_by == "kubesentinel-operator"
        # Reached the executor at all -- never stuck at APPROVED only.
        assert action.status in (
            ActionStatus.EXECUTED,
            ActionStatus.EXECUTION_FAILED,
            ActionStatus.VERIFYING,
            ActionStatus.VERIFIED_RECOVERED,
            ActionStatus.VERIFICATION_FAILED,
            ActionStatus.VERIFICATION_INCONCLUSIVE,
        )

    def test_controlled_auto_does_not_touch_an_ineligible_pending_action(self, repo):
        # LOW severity never clears PolicyEngine's own auto-execution bar --
        # see policy/engine.py::_auto_execution_eligible.
        incident = _incident_with_pending_action(severity=IncidentSeverity.LOW)
        repo.save(incident)
        clients = _clients()
        controller = RemediationPolicyController(_base_settings(), clients, repo)

        controller.reconcile(_cr(mode="CONTROLLED_AUTO"))

        saved = repo.get(incident.incident_id)
        assert saved.actions[0].status == ActionStatus.PENDING_APPROVAL

    def test_rollback_and_scale_always_require_approval_even_in_controlled_auto(self, repo):
        incident = _incident_with_pending_action(action_type=ActionType.SCALE_DEPLOYMENT)
        repo.save(incident)
        clients = _clients()
        controller = RemediationPolicyController(_base_settings(), clients, repo)

        controller.reconcile(_cr(mode="CONTROLLED_AUTO"))

        saved = repo.get(incident.incident_id)
        assert saved.actions[0].status == ActionStatus.PENDING_APPROVAL

    def test_only_reconciles_incidents_in_the_crs_own_namespace(self, repo):
        other_ns_incident = _incident_with_pending_action(namespace="other-namespace")
        repo.save(other_ns_incident)
        clients = _clients()
        controller = RemediationPolicyController(_base_settings(), clients, repo)

        controller.reconcile(_cr(mode="CONTROLLED_AUTO", namespace="kubesentinel-lab"))

        saved = repo.get(other_ns_incident.incident_id)
        assert saved.actions[0].status == ActionStatus.PENDING_APPROVAL

    def test_patches_status_with_pending_and_approved_counts(self, repo):
        incident = _incident_with_pending_action()
        repo.save(incident)
        clients = _clients()
        controller = RemediationPolicyController(_base_settings(), clients, repo)

        controller.reconcile(_cr(mode="CONTROLLED_AUTO", name="my-policy"))

        clients.custom_objects.patch_namespaced_custom_object_status.assert_called_once()
        call = clients.custom_objects.patch_namespaced_custom_object_status.call_args
        assert call.kwargs["name"] == "my-policy"
        status = call.kwargs["body"]["status"]
        assert status["pendingActionsSeen"] == 1
        assert status["actionsAutoApproved"] == 1

    def test_invalid_spec_patches_status_with_an_error_message_and_does_not_raise(self, repo):
        clients = _clients()
        controller = RemediationPolicyController(_base_settings(), clients, repo)

        controller.reconcile({"metadata": {"name": "bad-policy", "namespace": "kubesentinel-lab"}, "spec": {}})

        call = clients.custom_objects.patch_namespaced_custom_object_status.call_args
        assert "invalid spec" in call.kwargs["body"]["status"]["message"]

    def test_no_pending_actions_is_a_safe_no_op(self, repo):
        clients = _clients()
        controller = RemediationPolicyController(_base_settings(), clients, repo)

        controller.reconcile(_cr(mode="CONTROLLED_AUTO"))

        call = clients.custom_objects.patch_namespaced_custom_object_status.call_args
        status = call.kwargs["body"]["status"]
        assert status["pendingActionsSeen"] == 0
        assert status["actionsAutoApproved"] == 0

from datetime import datetime, timezone
from unittest.mock import MagicMock

from kubernetes.client import V1DeploymentCondition
from kubernetes.client.rest import ApiException

from kubesentinel.models.action import ActionTarget, ActionType, RemediationAction, VerificationStatus
from kubesentinel.verification.verifier import RecoveryVerifier
from tests.unit.factories import make_deployment


def _target(resource_uid: str = "deploy-uid-1234") -> ActionTarget:
    return ActionTarget(
        namespace="kubesentinel-lab", resource_type="Deployment", resource_name="test-deployment",
        resource_uid=resource_uid,
    )


def _action(action_type: ActionType = ActionType.RESTART_DEPLOYMENT) -> RemediationAction:
    return RemediationAction(
        action_id="action-1", incident_id="incident-1", action_type=action_type, target=_target(),
        rationale="test", proposed_at=datetime.now(timezone.utc), executed_at=datetime.now(timezone.utc),
    )


def test_recovered_when_deployment_is_fully_ready():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(
        desired_replicas=3, ready_replicas=3, available_replicas=3, updated_replicas=3
    )
    verifier = RecoveryVerifier(apps, deadline_seconds=1.0, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action())

    assert outcome.status == VerificationStatus.RECOVERED
    assert outcome.recovered is True


def test_failed_when_rollout_has_definitively_timed_out():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(
        desired_replicas=3,
        ready_replicas=0,
        conditions=[V1DeploymentCondition(type="Progressing", status="False", reason="ProgressDeadlineExceeded")],
    )
    verifier = RecoveryVerifier(apps, deadline_seconds=1.0, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action())

    assert outcome.status == VerificationStatus.FAILED
    assert outcome.recovered is False


def test_transient_available_false_does_not_count_as_failed():
    # Regression test for a bug found live: a routine SCALE_DEPLOYMENT 2->3 on a
    # perfectly healthy Deployment was reported FAILED because, for one poll,
    # Kubernetes reported Available=False/MinimumReplicasUnavailable -- entirely
    # normal while the one new replica hadn't started yet. This must fall
    # through to INCONCLUSIVE (not yet recovered, but not definitively stuck
    # either), never straight to FAILED.
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(
        desired_replicas=3,
        ready_replicas=2,
        available_replicas=2,
        conditions=[V1DeploymentCondition(type="Available", status="False", reason="MinimumReplicasUnavailable")],
    )
    verifier = RecoveryVerifier(apps, deadline_seconds=0.05, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action(action_type=ActionType.SCALE_DEPLOYMENT))

    assert outcome.status == VerificationStatus.INCONCLUSIVE


def test_failed_when_deployment_no_longer_exists():
    apps = MagicMock()
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    verifier = RecoveryVerifier(apps, deadline_seconds=1.0, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action())

    assert outcome.status == VerificationStatus.FAILED
    assert "no longer exists" in outcome.reason


def test_restart_requires_updated_replicas_not_just_ready_and_available():
    # Regression test for a bug found live: during a rollout, Kubernetes keeps
    # old Pods serving until new ones are ready, so ready/available can look
    # fully satisfied via the *old* ReplicaSet well before the new one is up.
    # For RESTART/ROLLBACK specifically, "recovered" must mean the rollout
    # itself completed (updatedReplicas caught up too), not just that some Pods
    # (possibly the old ones) are ready.
    apps = MagicMock()
    mid_rollout = make_deployment(
        desired_replicas=2, ready_replicas=2, available_replicas=2, updated_replicas=1
    )
    apps.read_namespaced_deployment.return_value = mid_rollout
    verifier = RecoveryVerifier(apps, deadline_seconds=0.05, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action(action_type=ActionType.RESTART_DEPLOYMENT))

    # Not confirmed recovered (rollout incomplete) and no failure signal either
    # -- correctly falls through to INCONCLUSIVE rather than a false RECOVERED.
    assert outcome.status == VerificationStatus.INCONCLUSIVE


def test_restart_recovers_once_updated_replicas_catches_up():
    apps = MagicMock()
    mid_rollout = make_deployment(
        desired_replicas=2, ready_replicas=2, available_replicas=2, updated_replicas=1
    )
    rollout_complete = make_deployment(
        desired_replicas=2, ready_replicas=2, available_replicas=2, updated_replicas=2
    )
    apps.read_namespaced_deployment.side_effect = [mid_rollout, rollout_complete]
    verifier = RecoveryVerifier(apps, deadline_seconds=1.0, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action(action_type=ActionType.RESTART_DEPLOYMENT))

    assert outcome.status == VerificationStatus.RECOVERED


def test_scale_does_not_require_updated_replicas():
    # SCALE_DEPLOYMENT never starts a new rollout -- the simpler aggregate check
    # is correct for it, and updated_replicas lagging must not block it.
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(
        desired_replicas=2, ready_replicas=2, available_replicas=2, updated_replicas=0
    )
    verifier = RecoveryVerifier(apps, deadline_seconds=1.0, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action(action_type=ActionType.SCALE_DEPLOYMENT))

    assert outcome.status == VerificationStatus.RECOVERED


def test_inconclusive_when_deadline_exceeded_with_no_clear_signal():
    apps = MagicMock()
    # Not fully ready, but no confirmed failure signal either -- genuinely ambiguous.
    apps.read_namespaced_deployment.return_value = make_deployment(desired_replicas=3, ready_replicas=1)
    verifier = RecoveryVerifier(apps, deadline_seconds=0.05, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action())

    assert outcome.status == VerificationStatus.INCONCLUSIVE
    assert outcome.recovered is False


def test_inconclusive_on_unexpected_api_error_not_raised():
    apps = MagicMock()
    apps.read_namespaced_deployment.side_effect = ApiException(status=500, reason="Internal Server Error")
    verifier = RecoveryVerifier(apps, deadline_seconds=1.0, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action())

    assert outcome.status == VerificationStatus.INCONCLUSIVE


def test_inconclusive_on_unexpected_exception_not_raised():
    apps = MagicMock()
    apps.read_namespaced_deployment.side_effect = RuntimeError("boom")
    verifier = RecoveryVerifier(apps, deadline_seconds=1.0, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action())

    assert outcome.status == VerificationStatus.INCONCLUSIVE
    assert "boom" in outcome.reason


def test_advisory_action_is_always_recovered_without_an_api_call():
    apps = MagicMock()
    verifier = RecoveryVerifier(apps, deadline_seconds=1.0, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action(action_type=ActionType.RECOMMEND_RESOURCE_ADJUSTMENT))

    assert outcome.status == VerificationStatus.RECOVERED
    apps.read_namespaced_deployment.assert_not_called()


def test_recovers_eventually_after_initially_ambiguous_polls():
    apps = MagicMock()
    ambiguous = make_deployment(desired_replicas=3, ready_replicas=1)
    recovered = make_deployment(desired_replicas=3, ready_replicas=3, available_replicas=3, updated_replicas=3)
    apps.read_namespaced_deployment.side_effect = [ambiguous, ambiguous, recovered]
    verifier = RecoveryVerifier(apps, deadline_seconds=5.0, poll_interval_seconds=0.01)

    outcome = verifier.verify(_action())

    assert outcome.status == VerificationStatus.RECOVERED
    assert outcome.details["attempts"] == 3

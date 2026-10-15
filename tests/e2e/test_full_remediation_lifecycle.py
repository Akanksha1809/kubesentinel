"""End-to-end: the full diagnose(-equivalent) -> policy -> plan -> approve ->
execute -> verify -> audit lifecycle, driven entirely through
`RemediationService` exactly as the API/CLI do, against the real kind
cluster's `remediation-test` Deployment -- not a mocked executor, not a faked
verification outcome.

This is the automated, repeatable version of the manual live verification
already documented in REMEDIATION_SAFETY.md's "Verified live against the real
kind cluster" section (scale/restart/rollback exercised by hand across
Milestones 4-6) -- TESTING.md flagged turning that into an actual e2e test as
Milestone 9 work.

Mutates the real `remediation-test` Deployment (scales 2 -> 3 -> back to 2).
The `ensure_remediation_test_deployment` fixture requires it to start at its
documented steady state (2/2) and this test always restores that state,
including on failure, via a `finally` block that uses a direct API call as a
last resort if the "proper" propose/approve/execute path itself is what
failed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from kubesentinel.config import PolicyMode, Settings
from kubesentinel.models.action import ActionStatus, ActionType, VerificationStatus
from kubesentinel.models.incident import Incident, IncidentSeverity, IncidentStatus, ResourceRef
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from kubesentinel.policy.engine import PolicyEngine
from kubesentinel.remediation.executor import RemediationExecutor
from kubesentinel.remediation.service import RemediationService
from kubesentinel.verification.verifier import RecoveryVerifier
from tests.conftest import LAB_NAMESPACE, REMEDIATION_TEST_DEPLOYMENT

pytestmark = pytest.mark.usefixtures("lab_namespace_ready", "ensure_remediation_test_deployment")


def _build_service(live_cluster_clients, tmp_path) -> RemediationService:
    settings = Settings(
        policy_mode=PolicyMode.APPROVAL_REQUIRED,
        action_allowed_namespaces=LAB_NAMESPACE,
        action_allowed_resource_names=REMEDIATION_TEST_DEPLOYMENT,
        action_cooldown_seconds=0,
        verification_deadline_seconds=30.0,
        verification_poll_interval_seconds=1.0,
    )
    repo = SQLiteIncidentRepository(str(tmp_path / "e2e.db"))
    policy = PolicyEngine(settings)
    executor = RemediationExecutor(live_cluster_clients.apps)
    verifier = RecoveryVerifier(
        live_cluster_clients.apps,
        deadline_seconds=settings.verification_deadline_seconds,
        poll_interval_seconds=settings.verification_poll_interval_seconds,
    )
    return RemediationService(policy, executor, verifier, repo, settings)


def _incident_for_remediation_test(live_cluster_clients) -> Incident:
    live = live_cluster_clients.apps.read_namespaced_deployment(
        name=REMEDIATION_TEST_DEPLOYMENT, namespace=LAB_NAMESPACE
    )
    now = datetime.now(timezone.utc)
    return Incident(
        incident_id=str(uuid4()),
        resource=ResourceRef(
            namespace=LAB_NAMESPACE,
            kind="Deployment",
            name=REMEDIATION_TEST_DEPLOYMENT,
            uid=live.metadata.uid,
        ),
        category="MANUAL_E2E_TEST",
        severity=IncidentSeverity.LOW,
        status=IncidentStatus.DIAGNOSED,
        first_observed_at=now,
        last_observed_at=now,
    )


def _scale(service: RemediationService, incident: Incident, replicas: int) -> None:
    action = service.propose(
        incident,
        ActionType.SCALE_DEPLOYMENT,
        rationale=f"e2e lifecycle test: scale to {replicas}",
        parameters={"replicas": replicas},
        target_desired_replicas=replicas,
    )
    assert action.status == ActionStatus.PENDING_APPROVAL, (
        f"SCALE_DEPLOYMENT always requires approval -- got {action.status}"
    )

    approved = service.approve(incident, action.action_id, approved_by="e2e-test")
    assert approved.status == ActionStatus.APPROVED

    executed = service.execute(incident, action.action_id)
    assert executed.status == ActionStatus.VERIFIED_RECOVERED, (
        f"expected a real scale to {replicas} to be confirmed recovered against the live "
        f"cluster, got {executed.status} (verification: {executed.verification})"
    )
    assert executed.verification is not None
    assert executed.verification.status == VerificationStatus.RECOVERED


def test_propose_approve_execute_verify_scale_up_and_back_down(live_cluster_clients, tmp_path):
    service = _build_service(live_cluster_clients, tmp_path)
    incident = _incident_for_remediation_test(live_cluster_clients)

    try:
        _scale(service, incident, 3)

        live_after_up = live_cluster_clients.apps.read_namespaced_deployment(
            name=REMEDIATION_TEST_DEPLOYMENT, namespace=LAB_NAMESPACE
        )
        assert live_after_up.spec.replicas == 3, "verified recovered, but the live Deployment isn't actually at 3"

        _scale(service, incident, 2)

        live_after_down = live_cluster_clients.apps.read_namespaced_deployment(
            name=REMEDIATION_TEST_DEPLOYMENT, namespace=LAB_NAMESPACE
        )
        assert live_after_down.spec.replicas == 2

        assert len(incident.actions) == 2
        assert [a.status for a in incident.actions] == [
            ActionStatus.VERIFIED_RECOVERED,
            ActionStatus.VERIFIED_RECOVERED,
        ]
        audit_event_types = [entry.event_type for entry in incident.audit_log]
        assert audit_event_types.count("remediation_proposed") == 2
        assert audit_event_types.count("remediation_approved") == 2
    finally:
        # Always restore the documented steady state (2/2), even on failure --
        # the "proper" path above is itself what might have failed, so fall
        # back to a direct API call rather than another propose/approve/execute
        # cycle that could fail the same way.
        live_cluster_clients.apps.patch_namespaced_deployment_scale(
            name=REMEDIATION_TEST_DEPLOYMENT,
            namespace=LAB_NAMESPACE,
            body={"spec": {"replicas": 2}},
        )

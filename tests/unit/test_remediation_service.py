import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kubesentinel.config import PolicyMode, Settings
from kubesentinel.models.action import ActionStatus, ActionTarget, ActionType
from kubesentinel.policy.decision import PolicyOutcome
from kubesentinel.models.diagnosis import Confidence, DiagnosisResult, Hypothesis, ReasonCode
from kubesentinel.models.incident import Incident, IncidentSeverity, IncidentStatus, ResourceRef
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from kubesentinel.policy.engine import PolicyEngine
from kubesentinel.remediation.executor import RemediationExecutor
from kubesentinel.remediation.service import (
    ActionNotFoundError,
    InvalidActionStateError,
    RemediationService,
)
from kubesentinel.verification.verifier import RecoveryVerifier
from tests.unit.factories import make_deployment


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteIncidentRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _settings(**overrides) -> Settings:
    defaults = dict(
        policy_mode=PolicyMode.APPROVAL_REQUIRED,
        action_allowed_namespaces="kubesentinel-lab",
        action_window_seconds=600,
        max_actions_per_incident=3,
        max_actions_per_window=10,
        action_cooldown_seconds=60,
        # Fast verification polling so tests that reach INCONCLUSIVE (deadline
        # exceeded) don't actually wait around.
        verification_deadline_seconds=0.05,
        verification_poll_interval_seconds=0.01,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _incident(
    severity: IncidentSeverity = IncidentSeverity.HIGH,
    confidence: Confidence = Confidence.HIGH,
    auto_remediation_permitted: bool = False,
) -> Incident:
    now = datetime.now(timezone.utc)
    hypothesis = Hypothesis(
        rule_name="test_rule",
        reason_code=ReasonCode.CRASH_LOOP_BACKOFF,
        confidence=confidence,
        explanation="test",
        auto_remediation_permitted=auto_remediation_permitted,
    )
    return Incident(
        incident_id="incident-1",
        resource=ResourceRef(
            namespace="kubesentinel-lab", kind="Deployment", name="test-deployment", uid="deploy-uid-1234"
        ),
        category="CRASH_LOOP_BACKOFF",
        severity=severity,
        status=IncidentStatus.DIAGNOSED,
        first_observed_at=now,
        last_observed_at=now,
        diagnosis_history=[DiagnosisResult(performed_at=now, hypotheses=[hypothesis])],
    )


def _service(repo, settings) -> tuple[RemediationService, MagicMock]:
    apps = MagicMock()
    # Healthy by default (ready == desired) so the verifier resolves to RECOVERED
    # quickly on the success path, without waiting out the deadline. Tests that
    # need a different post-action state override this return_value themselves.
    apps.read_namespaced_deployment.return_value = make_deployment(
        uid="deploy-uid-1234", desired_replicas=3, ready_replicas=3, available_replicas=3, updated_replicas=3
    )
    apps.patch_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    executor = RemediationExecutor(apps)
    verifier = RecoveryVerifier(
        apps,
        deadline_seconds=settings.verification_deadline_seconds,
        poll_interval_seconds=settings.verification_poll_interval_seconds,
    )
    service = RemediationService(PolicyEngine(settings), executor, verifier, repo, settings)
    return service, apps


def test_dry_run_never_creates_or_persists_an_action(repo):
    service, apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()

    decision = service.dry_run(incident, ActionType.RESTART_DEPLOYMENT)

    assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL
    assert incident.actions == []
    assert repo.get(incident.incident_id) is None
    apps.patch_namespaced_deployment.assert_not_called()


def test_dry_run_reflects_real_mode_and_allowlist(repo):
    service, _ = _service(repo, _settings(policy_mode=PolicyMode.OBSERVE))
    incident = _incident()

    decision = service.dry_run(incident, ActionType.RESTART_DEPLOYMENT)

    assert decision.outcome == PolicyOutcome.DENIED
    assert "OBSERVE" in decision.reason


def test_propose_in_approval_required_mode_leaves_action_pending(repo):
    service, apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()

    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")

    assert action.status == ActionStatus.PENDING_APPROVAL
    apps.patch_namespaced_deployment.assert_not_called()
    assert incident.actions == [action]


def test_propose_in_observe_mode_is_rejected_immediately(repo):
    service, apps = _service(repo, _settings(policy_mode=PolicyMode.OBSERVE))
    incident = _incident()

    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")

    assert action.status == ActionStatus.REJECTED
    assert "denied by policy" in action.execution_error
    apps.patch_namespaced_deployment.assert_not_called()


def test_propose_persists_the_incident(repo):
    service, _ = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()

    service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")

    reloaded = repo.get(incident.incident_id)
    assert len(reloaded.actions) == 1


def test_propose_adds_an_audit_entry(repo):
    service, _ = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()

    service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")

    assert any(a.event_type == "remediation_proposed" for a in incident.audit_log)


def test_controlled_auto_allowed_action_executes_immediately(repo):
    settings = _settings(policy_mode=PolicyMode.CONTROLLED_AUTO)
    service, apps = _service(repo, settings)
    incident = _incident(
        severity=IncidentSeverity.HIGH, confidence=Confidence.HIGH, auto_remediation_permitted=True
    )

    action = service.propose(
        incident, ActionType.RESTART_DEPLOYMENT, rationale="auto", target_desired_replicas=3
    )

    assert action.status == ActionStatus.VERIFIED_RECOVERED
    assert action.verification.status.value == "RECOVERED"
    assert action.approved_by == "policy-engine(CONTROLLED_AUTO)"
    apps.patch_namespaced_deployment.assert_called_once()


def test_controlled_auto_still_requires_approval_when_rule_denies_auto_remediation(repo):
    # The real-world default: no rule through Milestone 2 sets
    # auto_remediation_permitted=True.
    settings = _settings(policy_mode=PolicyMode.CONTROLLED_AUTO)
    service, apps = _service(repo, settings)
    incident = _incident(auto_remediation_permitted=False)

    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="auto", target_desired_replicas=3)

    assert action.status == ActionStatus.PENDING_APPROVAL
    apps.patch_namespaced_deployment.assert_not_called()


def test_approve_marks_approved_without_executing(repo):
    service, apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()
    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")

    approved = service.approve(incident, action.action_id, approved_by="akanksha")

    assert approved.status == ActionStatus.APPROVED
    assert approved.approved_by == "akanksha"
    apps.patch_namespaced_deployment.assert_not_called()


def test_execute_runs_an_approved_action(repo):
    service, apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()
    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")
    service.approve(incident, action.action_id, approved_by="akanksha")

    executed = service.execute(incident, action.action_id)

    assert executed.status == ActionStatus.VERIFIED_RECOVERED
    apps.patch_namespaced_deployment.assert_called_once()


def test_execute_rejects_an_action_that_is_not_approved(repo):
    service, _ = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()
    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")

    with pytest.raises(InvalidActionStateError):
        service.execute(incident, action.action_id)  # still PENDING_APPROVAL, not APPROVED


def test_execute_unknown_action_id_raises(repo):
    service, _ = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()

    with pytest.raises(ActionNotFoundError):
        service.execute(incident, "does-not-exist")


def test_approve_rejects_an_already_approved_action(repo):
    service, _ = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()
    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")
    service.approve(incident, action.action_id, approved_by="akanksha")

    with pytest.raises(InvalidActionStateError):
        service.approve(incident, action.action_id, approved_by="akanksha")


def test_approve_unknown_action_id_raises(repo):
    service, _ = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()

    with pytest.raises(ActionNotFoundError):
        service.approve(incident, "does-not-exist", approved_by="akanksha")


def test_reject_sets_rejected_status_and_does_not_execute(repo):
    service, apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()
    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")

    rejected = service.reject(incident, action.action_id, rejected_by="akanksha", reason="not needed")

    assert rejected.status == ActionStatus.REJECTED
    assert "not needed" in rejected.execution_error
    apps.patch_namespaced_deployment.assert_not_called()


def test_execution_failure_is_recorded_without_raising(repo):
    service, apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    apps.read_namespaced_deployment.return_value = make_deployment(uid="a-different-uid")
    incident = _incident()
    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")
    service.approve(incident, action.action_id, approved_by="akanksha")

    result = service.execute(incident, action.action_id)

    assert result.status == ActionStatus.EXECUTION_FAILED
    assert "changed" in result.execution_error
    # Execution never happened, so verification must never run either.
    assert result.verification is None


def test_verification_detects_failure_after_action_executes(repo):
    from kubernetes.client import V1DeploymentCondition

    service, apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    healthy_for_identity_check = make_deployment(uid="deploy-uid-1234")
    # ProgressDeadlineExceeded, not a transient Available=False -- the latter is
    # legitimately normal mid-rollout and must not be treated as a definitive
    # failure (see test_recovery_verifier.py's dedicated regression test for why).
    failing_after_restart = make_deployment(
        uid="deploy-uid-1234",
        desired_replicas=3,
        ready_replicas=0,
        conditions=[
            V1DeploymentCondition(type="Progressing", status="False", reason="ProgressDeadlineExceeded")
        ],
    )
    apps.read_namespaced_deployment.side_effect = [healthy_for_identity_check, failing_after_restart]
    incident = _incident()
    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")
    service.approve(incident, action.action_id, approved_by="akanksha")

    result = service.execute(incident, action.action_id)

    assert result.status == ActionStatus.VERIFICATION_FAILED
    assert result.verification.status.value == "FAILED"


def test_verification_inconclusive_when_deadline_exceeded_without_clear_signal(repo):
    service, apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    # Not fully ready, but no confirmed failure signal either -- genuinely ambiguous.
    apps.read_namespaced_deployment.return_value = make_deployment(
        uid="deploy-uid-1234", desired_replicas=3, ready_replicas=1
    )
    incident = _incident()
    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="manual test")
    service.approve(incident, action.action_id, approved_by="akanksha")

    result = service.execute(incident, action.action_id)

    assert result.status == ActionStatus.VERIFICATION_INCONCLUSIVE
    assert result.verification.status.value == "INCONCLUSIVE"


def test_advisory_action_is_verified_as_recovered_without_any_api_call(repo):
    # Advisory actions are always ALLOWED, even in APPROVAL_REQUIRED -- they never
    # mutate the cluster, so propose() already executes and verifies them inline,
    # with nothing left pending for approve() to do.
    service, apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    incident = _incident()
    action = service.propose(
        incident,
        ActionType.RECOMMEND_RESOURCE_ADJUSTMENT,
        rationale="manual test",
        parameters={"recommendation": "raise memory limit"},
    )

    assert action.status == ActionStatus.VERIFIED_RECOVERED
    apps.read_namespaced_deployment.assert_not_called()


def test_concurrent_proposals_never_exceed_the_per_incident_budget(repo):
    # Fast, mock-based regression test for TODO.md #20, found live in
    # Milestone 9's tests/integration/test_concurrent_remediation.py: propose()
    # reads recent_actions, decides, and persists in three separate steps --
    # without a lock, concurrent callers can all read "0 actions so far" before
    # any of them saves, bypassing the budget entirely. This mock-based version
    # runs under plain `./scripts/test.sh` (no live cluster needed) so a
    # regression here is caught on every normal test run, not only `--all`.
    concurrency = 10
    budget = 3
    service, _ = _service(
        repo,
        _settings(
            policy_mode=PolicyMode.APPROVAL_REQUIRED,
            max_actions_per_incident=budget,
            action_cooldown_seconds=0,
        ),
    )
    incident = _incident()
    repo.save(incident)

    barrier = threading.Barrier(concurrency)
    results: list[ActionStatus] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=5)
        action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="concurrency test")
        with results_lock:
            results.append(action.status)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    accepted = [s for s in results if s != ActionStatus.REJECTED]
    assert len(results) == concurrency
    assert len(accepted) == budget, f"expected exactly {budget} accepted under the budget, got {results}"


def _target() -> ActionTarget:
    return ActionTarget(
        namespace="kubesentinel-lab", resource_type="Deployment", resource_name="test-deployment",
        resource_uid="deploy-uid-1234",
    )


class TestStandaloneActions:
    """No owning Incident at all (docs/RESOURCE_OPTIMIZATION_DESIGN.md
    Section 14.1) -- e.g. applying a ResourceRecommendation."""

    def test_propose_standalone_persists_independently_of_any_incident(self, repo):
        service, _apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))

        action = service.propose_standalone(
            target=_target(),
            action_type=ActionType.ADJUST_RESOURCES,
            rationale="right-size",
            category="RESOURCE_OPTIMIZATION",
            parameters={"container_name": "app", "requests": {"cpu": "500m"}, "limits": {}},
        )

        assert action.incident_id is None
        assert action.status == ActionStatus.PENDING_APPROVAL
        assert repo.get_standalone_action(action.action_id) is not None

    def test_propose_standalone_denied_by_namespace_allowlist(self, repo):
        service, _apps = _service(
            repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED, action_allowed_namespaces="some-other-ns")
        )

        action = service.propose_standalone(
            target=_target(), action_type=ActionType.ADJUST_RESOURCES, rationale="test", category="RESOURCE_OPTIMIZATION"
        )

        assert action.status == ActionStatus.REJECTED

    def test_approve_then_execute_standalone_runs_through_executor_and_verifier(self, repo):
        service, apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
        action = service.propose_standalone(
            target=_target(),
            action_type=ActionType.ADJUST_RESOURCES,
            rationale="right-size",
            category="RESOURCE_OPTIMIZATION",
            parameters={"container_name": "app", "requests": {"cpu": "500m"}, "limits": {}},
        )
        assert action.status == ActionStatus.PENDING_APPROVAL

        approved = service.approve_standalone(action.action_id, approved_by="alice")
        assert approved.status == ActionStatus.APPROVED

        executed = service.execute_standalone(action.action_id, category="RESOURCE_OPTIMIZATION")
        assert executed.status == ActionStatus.VERIFIED_RECOVERED
        apps.patch_namespaced_deployment.assert_called_once()

    def test_reject_standalone(self, repo):
        service, _apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
        action = service.propose_standalone(
            target=_target(), action_type=ActionType.ADJUST_RESOURCES, rationale="test", category="RESOURCE_OPTIMIZATION"
        )

        rejected = service.reject_standalone(action.action_id, rejected_by="bob", reason="not needed")
        assert rejected.status == ActionStatus.REJECTED
        assert "bob" in rejected.execution_error

    def test_approve_standalone_requires_pending_approval_state(self, repo):
        service, _apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
        action = service.propose_standalone(
            target=_target(), action_type=ActionType.ADJUST_RESOURCES, rationale="test", category="RESOURCE_OPTIMIZATION"
        )
        service.approve_standalone(action.action_id, approved_by="alice")

        with pytest.raises(InvalidActionStateError):
            service.approve_standalone(action.action_id, approved_by="alice")

    def test_get_standalone_action_not_found_raises(self, repo):
        service, _apps = _service(repo, _settings())
        with pytest.raises(ActionNotFoundError):
            service.get_standalone_action("does-not-exist")

    def test_dry_run_standalone_has_zero_side_effects(self, repo):
        service, _apps = _service(repo, _settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))

        decision = service.dry_run_standalone(target=_target(), action_type=ActionType.ADJUST_RESOURCES)

        assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL
        assert repo.list_actions_since(datetime.now(timezone.utc).replace(year=2000)) == []

    def test_standalone_and_incident_driven_actions_share_the_same_budget(self, repo):
        # The per-window budget must bound every mutation this project makes,
        # not just the incident-driven ones -- list_actions_since unions both.
        service, _apps = _service(repo, _settings(max_actions_per_window=1, action_window_seconds=600))
        incident = _incident()

        first = service.propose_standalone(
            target=_target(), action_type=ActionType.ADJUST_RESOURCES, rationale="first", category="RESOURCE_OPTIMIZATION"
        )
        assert first.status != ActionStatus.REJECTED

        second = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="second")
        assert second.status == ActionStatus.REJECTED
        assert "per-window budget" in second.execution_error

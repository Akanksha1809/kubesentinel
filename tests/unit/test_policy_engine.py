from datetime import datetime, timedelta, timezone

import pytest

from kubesentinel.config import PolicyMode, Settings
from kubesentinel.models.action import ActionStatus, ActionTarget, ActionType, RemediationAction
from kubesentinel.models.diagnosis import Confidence
from kubesentinel.models.incident import IncidentSeverity
from kubesentinel.policy.decision import PolicyOutcome
from kubesentinel.policy.engine import PolicyEngine, PolicyRequest

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    defaults = dict(
        policy_mode=PolicyMode.CONTROLLED_AUTO,
        action_allowed_namespaces="kubesentinel-lab",
        action_allowed_resource_names="",
        max_actions_per_incident=3,
        max_actions_per_window=10,
        action_window_seconds=600,
        action_cooldown_seconds=60,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _target(
    namespace: str = "kubesentinel-lab",
    resource_type: str = "Deployment",
    resource_name: str = "crashloop-demo",
    resource_uid: str = "uid-1",
) -> ActionTarget:
    return ActionTarget(
        namespace=namespace, resource_type=resource_type, resource_name=resource_name, resource_uid=resource_uid
    )


def _request(
    action_type: ActionType = ActionType.RESTART_DEPLOYMENT,
    target: ActionTarget | None = None,
    incident_id: str = "incident-1",
    incident_severity: IncidentSeverity = IncidentSeverity.HIGH,
    hypothesis_confidence: Confidence = Confidence.HIGH,
    hypothesis_auto_remediation_permitted: bool = True,
    target_desired_replicas: int | None = 3,
    recent_actions: list[RemediationAction] | None = None,
    now: datetime = NOW,
) -> PolicyRequest:
    return PolicyRequest(
        action_type=action_type,
        target=target or _target(),
        incident_id=incident_id,
        incident_severity=incident_severity,
        hypothesis_confidence=hypothesis_confidence,
        hypothesis_auto_remediation_permitted=hypothesis_auto_remediation_permitted,
        now=now,
        target_desired_replicas=target_desired_replicas,
        recent_actions=recent_actions or [],
    )


def _action(
    action_type: ActionType = ActionType.RESTART_DEPLOYMENT,
    target: ActionTarget | None = None,
    incident_id: str = "incident-1",
    proposed_at: datetime = NOW,
    status: ActionStatus = ActionStatus.PROPOSED,
) -> RemediationAction:
    return RemediationAction(
        action_id="action-1",
        incident_id=incident_id,
        action_type=action_type,
        target=target or _target(),
        rationale="test",
        proposed_at=proposed_at,
        status=status,
    )


def test_advisory_actions_are_always_allowed_even_in_observe_mode():
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.OBSERVE))
    decision = engine.evaluate(_request(action_type=ActionType.RECOMMEND_RESOURCE_ADJUSTMENT))
    assert decision.outcome == PolicyOutcome.ALLOWED


def test_observe_mode_denies_everything_else():
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.OBSERVE))
    decision = engine.evaluate(_request())
    assert decision.outcome == PolicyOutcome.DENIED
    assert "OBSERVE" in decision.reason


def test_namespace_not_in_allowlist_is_denied():
    engine = PolicyEngine(_settings(action_allowed_namespaces="other-namespace"))
    decision = engine.evaluate(_request(target=_target(namespace="kubesentinel-lab")))
    assert decision.outcome == PolicyOutcome.DENIED
    assert "namespace" in decision.reason


def test_resource_name_allowlist_denies_unlisted_name():
    engine = PolicyEngine(_settings(action_allowed_resource_names="only-this-one"))
    decision = engine.evaluate(_request(target=_target(resource_name="crashloop-demo")))
    assert decision.outcome == PolicyOutcome.DENIED
    assert "resource-name allowlist" in decision.reason


def test_resource_name_allowlist_allows_listed_name():
    engine = PolicyEngine(_settings(action_allowed_resource_names="crashloop-demo,other"))
    decision = engine.evaluate(_request(target=_target(resource_name="crashloop-demo")))
    assert decision.outcome != PolicyOutcome.DENIED


def test_empty_resource_name_allowlist_means_no_additional_restriction():
    engine = PolicyEngine(_settings(action_allowed_resource_names=""))
    decision = engine.evaluate(_request(target=_target(resource_name="anything")))
    assert decision.outcome != PolicyOutcome.DENIED


def test_restart_deployment_against_a_pod_target_is_denied():
    engine = PolicyEngine(_settings())
    decision = engine.evaluate(_request(target=_target(resource_type="Pod")))
    assert decision.outcome == PolicyOutcome.DENIED
    assert "only applies to Deployment" in decision.reason


def test_cooldown_denies_a_repeat_action_on_the_same_target_too_soon():
    engine = PolicyEngine(_settings(action_cooldown_seconds=60))
    recent = [_action(proposed_at=NOW - timedelta(seconds=10))]
    decision = engine.evaluate(_request(recent_actions=recent))
    assert decision.outcome == PolicyOutcome.DENIED
    assert "cooldown" in decision.reason


def test_cooldown_does_not_apply_after_it_elapses():
    engine = PolicyEngine(_settings(action_cooldown_seconds=60))
    recent = [_action(proposed_at=NOW - timedelta(seconds=120))]
    decision = engine.evaluate(_request(recent_actions=recent))
    assert decision.outcome != PolicyOutcome.DENIED


def test_cooldown_does_not_apply_to_a_different_target():
    engine = PolicyEngine(_settings(action_cooldown_seconds=60))
    recent = [_action(proposed_at=NOW - timedelta(seconds=5), target=_target(resource_uid="uid-OTHER"))]
    decision = engine.evaluate(_request(recent_actions=recent, target=_target(resource_uid="uid-1")))
    assert decision.outcome != PolicyOutcome.DENIED


def test_cooldown_does_not_apply_to_a_different_action_type():
    engine = PolicyEngine(_settings(action_cooldown_seconds=60))
    recent = [_action(action_type=ActionType.ROLLBACK_DEPLOYMENT, proposed_at=NOW - timedelta(seconds=5))]
    decision = engine.evaluate(_request(action_type=ActionType.RESTART_DEPLOYMENT, recent_actions=recent))
    assert decision.outcome != PolicyOutcome.DENIED


def test_cooldown_ignores_a_previously_rejected_action():
    # Regression test for a bug found live: a REJECTED action never touched the
    # cluster, so it must not start a cooldown that blocks a later, otherwise-
    # valid attempt of the same action type on the same target.
    engine = PolicyEngine(_settings(action_cooldown_seconds=60))
    recent = [_action(proposed_at=NOW - timedelta(seconds=5), status=ActionStatus.REJECTED)]
    decision = engine.evaluate(_request(recent_actions=recent))
    assert decision.outcome != PolicyOutcome.DENIED


def test_per_incident_budget_exhausted_is_denied():
    engine = PolicyEngine(_settings(max_actions_per_incident=2))
    recent = [_action(incident_id="incident-1", proposed_at=NOW - timedelta(seconds=500)) for _ in range(2)]
    decision = engine.evaluate(_request(incident_id="incident-1", recent_actions=recent))
    assert decision.outcome == PolicyOutcome.DENIED
    assert "per-incident budget" in decision.reason


def test_per_incident_budget_excludes_rejected_actions():
    # Regression test for a bug found live: a few harmless rejections (e.g. from
    # an unrelated cooldown) must not permanently exhaust an incident's budget --
    # none of them ever actually touched the cluster.
    engine = PolicyEngine(_settings(max_actions_per_incident=2))
    recent = [
        _action(incident_id="incident-1", proposed_at=NOW - timedelta(seconds=i), status=ActionStatus.REJECTED)
        for i in range(5)
    ]
    decision = engine.evaluate(_request(incident_id="incident-1", recent_actions=recent))
    assert decision.outcome != PolicyOutcome.DENIED


def test_per_window_budget_excludes_rejected_actions():
    engine = PolicyEngine(_settings(max_actions_per_window=2, action_window_seconds=600))
    recent = [
        _action(incident_id=f"incident-{i}", proposed_at=NOW - timedelta(seconds=100), status=ActionStatus.REJECTED)
        for i in range(5)
    ]
    decision = engine.evaluate(_request(recent_actions=recent))
    assert decision.outcome != PolicyOutcome.DENIED


def test_per_incident_budget_does_not_count_other_incidents():
    engine = PolicyEngine(_settings(max_actions_per_incident=1))
    # A different target_uid avoids also tripping the cooldown check -- this test
    # isolates the per-incident budget count specifically.
    other_target = _target(resource_uid="uid-OTHER")
    recent = [_action(incident_id="incident-OTHER", target=other_target, proposed_at=NOW - timedelta(seconds=500))]
    decision = engine.evaluate(_request(incident_id="incident-1", recent_actions=recent))
    assert decision.outcome != PolicyOutcome.DENIED


def test_per_window_budget_exhausted_is_denied():
    engine = PolicyEngine(_settings(max_actions_per_window=2, action_window_seconds=600))
    recent = [
        _action(incident_id=f"incident-{i}", proposed_at=NOW - timedelta(seconds=100)) for i in range(2)
    ]
    decision = engine.evaluate(_request(recent_actions=recent))
    assert decision.outcome == PolicyOutcome.DENIED
    assert "per-window budget" in decision.reason


def test_per_window_budget_ignores_actions_outside_the_window():
    engine = PolicyEngine(_settings(max_actions_per_window=1, action_window_seconds=600))
    recent = [_action(incident_id="incident-OLD", proposed_at=NOW - timedelta(seconds=9999))]
    decision = engine.evaluate(_request(recent_actions=recent))
    assert decision.outcome != PolicyOutcome.DENIED


def test_approval_required_mode_requires_approval_for_everything_that_passes_deny_checks():
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.APPROVAL_REQUIRED))
    decision = engine.evaluate(_request())
    assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL


def test_rollback_always_requires_approval_even_in_controlled_auto():
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.CONTROLLED_AUTO))
    decision = engine.evaluate(_request(action_type=ActionType.ROLLBACK_DEPLOYMENT))
    assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL
    assert "always requires approval" in decision.reason


def test_scale_always_requires_approval_even_in_controlled_auto():
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.CONTROLLED_AUTO))
    decision = engine.evaluate(_request(action_type=ActionType.SCALE_DEPLOYMENT))
    assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL


class TestAdjustResources:
    def test_always_requires_approval_even_in_controlled_auto(self):
        engine = PolicyEngine(_settings(policy_mode=PolicyMode.CONTROLLED_AUTO))
        decision = engine.evaluate(
            _request(
                action_type=ActionType.ADJUST_RESOURCES,
                incident_id=None,
                incident_severity=None,
                hypothesis_confidence=Confidence.INCONCLUSIVE,
                hypothesis_auto_remediation_permitted=False,
            )
        )
        assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL

    def test_against_a_pod_target_is_denied(self):
        engine = PolicyEngine(_settings())
        decision = engine.evaluate(
            _request(action_type=ActionType.ADJUST_RESOURCES, target=_target(resource_type="Pod"))
        )
        assert decision.outcome == PolicyOutcome.DENIED
        assert "only applies to Deployment" in decision.reason


class TestStandaloneActionsWithNoIncident:
    """PolicyRequest.incident_id/incident_severity are None for a
    standalone action (docs/RESOURCE_OPTIMIZATION_DESIGN.md Section
    14.1) -- e.g. applying a ResourceRecommendation."""

    def test_per_incident_budget_check_is_a_no_op_without_an_incident(self):
        # Even with an absurdly low budget, a request with no incident_id
        # must never be denied by the per-incident check specifically --
        # there is no incident to bound against.
        engine = PolicyEngine(_settings(max_actions_per_incident=0, policy_mode=PolicyMode.APPROVAL_REQUIRED))
        decision = engine.evaluate(
            _request(
                action_type=ActionType.ADJUST_RESOURCES,
                incident_id=None,
                incident_severity=None,
            )
        )
        assert "per-incident budget" not in decision.reason

    def test_per_window_budget_still_applies_without_an_incident(self):
        engine = PolicyEngine(_settings(max_actions_per_window=1, action_window_seconds=600))
        recent = [_action(incident_id=None, proposed_at=NOW - timedelta(seconds=100))]
        decision = engine.evaluate(
            _request(
                action_type=ActionType.ADJUST_RESOURCES,
                incident_id=None,
                incident_severity=None,
                recent_actions=recent,
            )
        )
        assert decision.outcome == PolicyOutcome.DENIED
        assert "per-window budget" in decision.reason

    def test_cooldown_still_applies_without_an_incident(self):
        engine = PolicyEngine(_settings(action_cooldown_seconds=60))
        recent = [
            _action(
                action_type=ActionType.ADJUST_RESOURCES, incident_id=None, proposed_at=NOW - timedelta(seconds=10)
            )
        ]
        decision = engine.evaluate(
            _request(
                action_type=ActionType.ADJUST_RESOURCES,
                incident_id=None,
                incident_severity=None,
                recent_actions=recent,
            )
        )
        assert decision.outcome == PolicyOutcome.DENIED
        assert "cooldown" in decision.reason

    def test_never_auto_execution_eligible_regardless_of_mode(self):
        # Even in CONTROLLED_AUTO with every DENY-capable check passing,
        # a standalone request's own INCONCLUSIVE confidence/False
        # auto_remediation_permitted already make it ineligible, before
        # even reaching the explicit _ALWAYS_REQUIRES_APPROVAL entry.
        engine = PolicyEngine(_settings(policy_mode=PolicyMode.CONTROLLED_AUTO))
        decision = engine.evaluate(
            _request(
                action_type=ActionType.RESTART_DEPLOYMENT,
                incident_id=None,
                incident_severity=None,
                hypothesis_confidence=Confidence.INCONCLUSIVE,
                hypothesis_auto_remediation_permitted=False,
            )
        )
        assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL


def test_controlled_auto_allows_restart_when_fully_eligible():
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.CONTROLLED_AUTO))
    decision = engine.evaluate(
        _request(
            action_type=ActionType.RESTART_DEPLOYMENT,
            incident_severity=IncidentSeverity.HIGH,
            hypothesis_confidence=Confidence.HIGH,
            hypothesis_auto_remediation_permitted=True,
            target_desired_replicas=3,
        )
    )
    assert decision.outcome == PolicyOutcome.ALLOWED


def test_controlled_auto_requires_approval_when_rule_itself_denies_auto_remediation():
    # This is the real-world default: every rule through Milestone 2 sets
    # auto_remediation_permitted=False on every hypothesis it produces. Severity,
    # confidence, and replica count being otherwise perfect must not override that.
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.CONTROLLED_AUTO))
    decision = engine.evaluate(
        _request(
            action_type=ActionType.RESTART_DEPLOYMENT,
            incident_severity=IncidentSeverity.HIGH,
            hypothesis_confidence=Confidence.HIGH,
            hypothesis_auto_remediation_permitted=False,
            target_desired_replicas=3,
        )
    )
    assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL


@pytest.mark.parametrize("severity", [IncidentSeverity.LOW, IncidentSeverity.MEDIUM])
def test_controlled_auto_requires_approval_below_high_severity(severity):
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.CONTROLLED_AUTO))
    decision = engine.evaluate(_request(incident_severity=severity))
    assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL


@pytest.mark.parametrize("confidence", [Confidence.MEDIUM, Confidence.LOW, Confidence.INCONCLUSIVE])
def test_controlled_auto_requires_approval_below_high_confidence(confidence):
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.CONTROLLED_AUTO))
    decision = engine.evaluate(_request(hypothesis_confidence=confidence))
    assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL


def test_controlled_auto_requires_approval_for_single_replica_deployment():
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.CONTROLLED_AUTO))
    decision = engine.evaluate(_request(target_desired_replicas=1))
    assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL
    assert "availability" in decision.reason or "doesn't meet the bar" in decision.reason


def test_controlled_auto_requires_approval_for_unknown_replica_count():
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.CONTROLLED_AUTO))
    decision = engine.evaluate(_request(target_desired_replicas=None))
    assert decision.outcome == PolicyOutcome.REQUIRES_APPROVAL


def test_every_denial_and_approval_reason_is_non_empty():
    engine = PolicyEngine(_settings(policy_mode=PolicyMode.OBSERVE))
    decision = engine.evaluate(_request())
    assert decision.reason.strip() != ""

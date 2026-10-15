from kubesentinel.diagnostics.rules.deployment import RolloutFailedRule, UnsatisfiedReplicasRule
from kubesentinel.incidents.health import has_failure_signal, is_confirmed_healthy
from kubesentinel.models.diagnosis import Confidence, ReasonCode
from kubesentinel.observer.inspector import deployment_evidence, resource_ref_from_deployment
from tests.unit.factories import make_deployment, progressing_condition


def test_rollout_failed_rule_fires_on_progress_deadline_exceeded():
    cond = progressing_condition(
        status="False", reason="ProgressDeadlineExceeded", message="deadline exceeded"
    )
    deployment = make_deployment(desired_replicas=3, ready_replicas=1, conditions=[cond])
    evidence = deployment_evidence(deployment)

    hypotheses = RolloutFailedRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.ROLLOUT_FAILED
    assert hypotheses[0].confidence == Confidence.HIGH


def test_rollout_failed_rule_does_not_fire_while_progressing_normally():
    cond = progressing_condition(status="True", reason="ReplicaSetUpdated")
    deployment = make_deployment(desired_replicas=3, ready_replicas=1, conditions=[cond])
    evidence = deployment_evidence(deployment)
    assert RolloutFailedRule().evaluate(evidence) == []


def test_unsatisfied_replicas_does_not_fire_when_fully_ready():
    deployment = make_deployment(desired_replicas=2, ready_replicas=2)
    evidence = deployment_evidence(deployment)
    assert UnsatisfiedReplicasRule().evaluate(evidence) == []


def test_unsatisfied_replicas_high_confidence_when_controller_considers_rollout_complete():
    cond = progressing_condition(status="True", reason="NewReplicaSetAvailable")
    deployment = make_deployment(desired_replicas=3, ready_replicas=1, conditions=[cond])
    evidence = deployment_evidence(deployment)

    hypotheses = UnsatisfiedReplicasRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.UNSATISFIED_REPLICAS
    assert hypotheses[0].confidence == Confidence.HIGH


def test_unsatisfied_replicas_high_confidence_when_available_condition_false():
    # Confirmed live: a Deployment whose pods start crashing after a successful
    # initial rollout can show Progressing=True/ReplicaSetUpdated (not yet, or no
    # longer, NewReplicaSetAvailable) while Available=False/MinimumReplicasUnavailable
    # -- this must still reach HIGH confidence via the Available condition alone.
    progressing = progressing_condition(status="True", reason="ReplicaSetUpdated")
    from kubernetes.client import V1DeploymentCondition

    available = V1DeploymentCondition(
        type="Available",
        status="False",
        reason="MinimumReplicasUnavailable",
        message="Deployment does not have minimum availability.",
    )
    deployment = make_deployment(desired_replicas=1, ready_replicas=0, conditions=[progressing, available])
    evidence = deployment_evidence(deployment)

    hypotheses = UnsatisfiedReplicasRule().evaluate(evidence)
    assert hypotheses[0].confidence == Confidence.HIGH


def test_unsatisfied_replicas_medium_confidence_for_single_observation_mid_rollout():
    cond = progressing_condition(status="True", reason="ReplicaSetUpdated")
    deployment = make_deployment(desired_replicas=3, ready_replicas=1, conditions=[cond])
    evidence = deployment_evidence(deployment)

    hypotheses = UnsatisfiedReplicasRule().evaluate(evidence)
    assert hypotheses[0].confidence == Confidence.MEDIUM
    assert hypotheses[0].missing_evidence


def test_unsatisfied_replicas_upgrades_to_high_confidence_when_sustained():
    cond = progressing_condition(status="True", reason="ReplicaSetUpdated")
    old_deployment = make_deployment(desired_replicas=3, ready_replicas=1, conditions=[cond])
    evidence = deployment_evidence(old_deployment)
    # Simulate an earlier observation 60s before the latest one, both showing a deficit.
    evidence[0].observed_at = evidence[0].observed_at.replace(microsecond=0)
    import copy
    from datetime import timedelta

    earlier = copy.deepcopy(evidence[0])
    earlier.observed_at = earlier.observed_at - timedelta(seconds=60)
    full_evidence = [earlier] + evidence

    hypotheses = UnsatisfiedReplicasRule().evaluate(full_evidence)
    assert hypotheses[0].confidence == Confidence.HIGH
    assert "sustained" in hypotheses[0].explanation


def test_deployment_evidence_extracts_replica_counts():
    deployment = make_deployment(desired_replicas=3, ready_replicas=2, available_replicas=2)
    evidence = deployment_evidence(deployment)
    replica_evidence = [e for e in evidence if e.source_field == "status.replicas"]
    assert len(replica_evidence) == 1
    assert replica_evidence[0].data == {
        "desired_replicas": 3,
        "ready_replicas": 2,
        "available_replicas": 2,
        "updated_replicas": 0,
        "unavailable_replicas": 0,
    }


def test_resource_ref_from_deployment():
    deployment = make_deployment(name="my-app", namespace="ns", uid="abc")
    ref = resource_ref_from_deployment(deployment)
    assert ref.kind == "Deployment"
    assert ref.name == "my-app"
    assert ref.uid == "abc"


def test_progress_deadline_exceeded_is_a_failure_signal():
    cond = progressing_condition(status="False", reason="ProgressDeadlineExceeded")
    evidence = deployment_evidence(make_deployment(desired_replicas=2, ready_replicas=0, conditions=[cond]))
    assert has_failure_signal(evidence) is True


def test_available_false_condition_is_a_failure_signal():
    from kubernetes.client import V1DeploymentCondition

    available = V1DeploymentCondition(
        type="Available", status="False", reason="MinimumReplicasUnavailable"
    )
    evidence = deployment_evidence(make_deployment(desired_replicas=1, ready_replicas=0, conditions=[available]))
    assert has_failure_signal(evidence) is True


def test_normal_progressing_replica_deficit_is_not_a_failure_signal():
    # Must not open an incident just because a rollout is normally still in progress.
    cond = progressing_condition(status="True", reason="ReplicaSetUpdated")
    evidence = deployment_evidence(make_deployment(desired_replicas=2, ready_replicas=1, conditions=[cond]))
    assert has_failure_signal(evidence) is False


def test_fully_ready_deployment_is_confirmed_healthy():
    evidence = deployment_evidence(make_deployment(desired_replicas=2, ready_replicas=2))
    assert is_confirmed_healthy(evidence) is True


def test_deficient_deployment_is_not_confirmed_healthy():
    evidence = deployment_evidence(make_deployment(desired_replicas=2, ready_replicas=1))
    assert is_confirmed_healthy(evidence) is False

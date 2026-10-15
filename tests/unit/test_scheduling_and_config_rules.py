from kubesentinel.diagnostics.rules.missing_config import MissingConfigReferenceRule
from kubesentinel.diagnostics.rules.scheduling import PendingSchedulingRule
from kubesentinel.models.diagnosis import Confidence, ReasonCode
from kubesentinel.observer.inspector import pod_evidence
from tests.unit.factories import make_pod, unschedulable_condition, waiting_container_status


def test_missing_config_reference_fires_on_create_container_config_error():
    cs = waiting_container_status(
        reason="CreateContainerConfigError", message='configmap "app-config" not found'
    )
    evidence = pod_evidence(make_pod(phase="Pending", container_statuses=[cs]))

    hypotheses = MissingConfigReferenceRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.MISSING_CONFIG_REFERENCE
    assert hypotheses[0].confidence == Confidence.HIGH
    assert "app-config" in hypotheses[0].explanation


def test_missing_config_reference_does_not_fire_on_unrelated_waiting_reason():
    cs = waiting_container_status(reason="CrashLoopBackOff")
    evidence = pod_evidence(make_pod(container_statuses=[cs]))
    assert MissingConfigReferenceRule().evaluate(evidence) == []


def test_pending_scheduling_classifies_insufficient_resources():
    cond = unschedulable_condition("0/1 nodes are available: 1 Insufficient cpu, 1 Insufficient memory.")
    evidence = pod_evidence(make_pod(phase="Pending", conditions=[cond]))

    hypotheses = PendingSchedulingRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.PENDING_INSUFFICIENT_RESOURCES
    assert hypotheses[0].confidence == Confidence.HIGH


def test_pending_scheduling_classifies_taint():
    cond = unschedulable_condition(
        "0/2 nodes are available: 2 node(s) had untolerated taint {key: value}."
    )
    evidence = pod_evidence(make_pod(phase="Pending", conditions=[cond]))

    hypotheses = PendingSchedulingRule().evaluate(evidence)
    assert hypotheses[0].reason_code == ReasonCode.PENDING_SCHEDULING_CONSTRAINT
    assert hypotheses[0].confidence == Confidence.HIGH


def test_pending_scheduling_classifies_node_affinity():
    cond = unschedulable_condition("0/1 nodes are available: 1 node(s) didn't match Pod's node affinity/selector.")
    evidence = pod_evidence(make_pod(phase="Pending", conditions=[cond]))

    hypotheses = PendingSchedulingRule().evaluate(evidence)
    assert hypotheses[0].reason_code == ReasonCode.PENDING_SCHEDULING_CONSTRAINT
    assert hypotheses[0].confidence == Confidence.HIGH


def test_pending_scheduling_falls_back_to_medium_confidence_for_unknown_message():
    cond = unschedulable_condition("0/1 nodes are available: something unexpected happened.")
    evidence = pod_evidence(make_pod(phase="Pending", conditions=[cond]))

    hypotheses = PendingSchedulingRule().evaluate(evidence)
    assert hypotheses[0].confidence == Confidence.MEDIUM
    assert hypotheses[0].missing_evidence


def test_pending_scheduling_does_not_fire_when_scheduled():
    cond = unschedulable_condition("")
    cond.status = "True"
    cond.reason = None
    evidence = pod_evidence(make_pod(conditions=[cond]))
    assert PendingSchedulingRule().evaluate(evidence) == []

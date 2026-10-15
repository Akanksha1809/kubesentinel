from kubesentinel.diagnostics.rules.probes import LivenessProbeFailingRule, ReadinessProbeFailingRule
from kubesentinel.incidents.health import has_failure_signal
from kubesentinel.models.diagnosis import ReasonCode
from kubesentinel.observer.inspector import event_evidence
from tests.unit.factories import make_event


def test_readiness_probe_failing_rule_fires():
    event = make_event(
        reason="Unhealthy", message="Readiness probe failed: HTTP probe failed with statuscode: 503"
    )
    evidence = [event_evidence(event)]

    hypotheses = ReadinessProbeFailingRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.READINESS_PROBE_FAILING


def test_readiness_probe_rule_ignores_liveness_message():
    event = make_event(reason="Unhealthy", message="Liveness probe failed: timeout")
    evidence = [event_evidence(event)]
    assert ReadinessProbeFailingRule().evaluate(evidence) == []


def test_liveness_probe_failing_rule_fires():
    event = make_event(reason="Unhealthy", message="Liveness probe failed: dial tcp timeout")
    evidence = [event_evidence(event)]

    hypotheses = LivenessProbeFailingRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.LIVENESS_PROBE_FAILING


def test_liveness_probe_rule_ignores_unrelated_warning_event():
    event = make_event(reason="FailedScheduling", message="0/1 nodes are available")
    evidence = [event_evidence(event)]
    assert LivenessProbeFailingRule().evaluate(evidence) == []


def test_unhealthy_event_is_a_failure_signal():
    event = make_event(reason="Unhealthy", message="Readiness probe failed: connection refused")
    evidence = [event_evidence(event)]
    assert has_failure_signal(evidence) is True


def test_unrelated_event_is_not_a_failure_signal():
    event = make_event(reason="Scheduled", message="Successfully assigned pod to node")
    evidence = [event_evidence(event)]
    assert has_failure_signal(evidence) is False

from kubesentinel.diagnostics.rules.mount import FailedMountRule
from kubesentinel.incidents.health import has_failure_signal
from kubesentinel.models.diagnosis import ReasonCode
from kubesentinel.observer.inspector import event_evidence
from tests.unit.factories import make_event


def test_failed_mount_fires_on_failed_mount_event():
    event = make_event(
        reason="FailedMount",
        message="Unable to attach or mount volumes: unmounted volumes=[data]",
        involved_object_kind="Pod",
    )
    evidence = [event_evidence(event)]

    hypotheses = FailedMountRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.FAILED_MOUNT
    assert hypotheses[0].confidence.value == "high"


def test_failed_mount_does_not_fire_on_unrelated_event():
    event = make_event(reason="Unhealthy", message="readiness probe failed", involved_object_kind="Pod")
    evidence = [event_evidence(event)]

    assert FailedMountRule().evaluate(evidence) == []


def test_failed_mount_event_is_a_failure_signal():
    # Without this, a Pod stuck FailedMount would never open an incident at
    # all -- no waiting/terminated container state ever reflects it, the
    # Event is the only evidence this failure mode produces.
    event = make_event(reason="FailedMount", message="mount failed", involved_object_kind="Pod")
    evidence = [event_evidence(event)]

    assert has_failure_signal(evidence) is True


def test_supporting_evidence_references_the_real_event_id():
    event = make_event(reason="FailedMount", message="mount failed", involved_object_kind="Pod")
    ev = event_evidence(event)
    hypotheses = FailedMountRule().evaluate([ev])

    assert hypotheses[0].supporting_evidence == [ev.id]

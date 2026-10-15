from kubesentinel.diagnostics.rules.pvc import PvcBindingFailureRule
from kubesentinel.incidents.health import has_failure_signal, is_confirmed_healthy
from kubesentinel.models.diagnosis import ReasonCode
from kubesentinel.observer.inspector import event_evidence, pvc_evidence, resource_ref_from_pvc
from tests.unit.factories import make_event, make_pvc


def test_pvc_binding_failure_fires_on_provisioning_failed_event():
    event = make_event(
        reason="ProvisioningFailed",
        message="storageclass.storage.k8s.io \"missing\" not found",
        involved_object_kind="PersistentVolumeClaim",
    )
    evidence = [event_evidence(event)]

    hypotheses = PvcBindingFailureRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.PVC_BINDING_FAILURE


def test_pvc_binding_failure_fires_on_failed_binding_event():
    event = make_event(
        reason="FailedBinding",
        message="no persistent volumes available for this claim",
        involved_object_kind="PersistentVolumeClaim",
    )
    evidence = [event_evidence(event)]
    assert len(PvcBindingFailureRule().evaluate(evidence)) == 1


def test_pending_phase_alone_is_not_a_failure_signal():
    # WaitForFirstConsumer storage classes leave a PVC correctly Pending until a Pod
    # references it -- must not be fabricated into a diagnosis.
    evidence = pvc_evidence(make_pvc(phase="Pending"))
    assert PvcBindingFailureRule().evaluate(evidence) == []
    assert has_failure_signal(evidence) is False


def test_provisioning_failed_event_is_a_failure_signal():
    event = make_event(
        reason="ProvisioningFailed", message="failed", involved_object_kind="PersistentVolumeClaim"
    )
    evidence = [event_evidence(event)]
    assert has_failure_signal(evidence) is True


def test_bound_pvc_is_confirmed_healthy():
    evidence = pvc_evidence(make_pvc(phase="Bound"))
    assert is_confirmed_healthy(evidence) is True


def test_pending_pvc_is_not_confirmed_healthy():
    evidence = pvc_evidence(make_pvc(phase="Pending"))
    assert is_confirmed_healthy(evidence) is False


def test_resource_ref_from_pvc():
    ref = resource_ref_from_pvc(make_pvc(name="data", namespace="ns", uid="abc"))
    assert ref.kind == "PersistentVolumeClaim"
    assert ref.name == "data"
    assert ref.uid == "abc"

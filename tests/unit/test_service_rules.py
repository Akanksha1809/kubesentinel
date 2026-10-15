from kubesentinel.diagnostics.rules.service import ServiceSelectorMismatchRule
from kubesentinel.incidents.health import has_failure_signal, is_confirmed_healthy
from kubesentinel.models.diagnosis import Confidence, ReasonCode
from kubesentinel.observer.inspector import endpointslice_evidence, resource_ref_from_service
from tests.unit.factories import make_endpointslice, make_service


def test_zero_endpoints_fires_medium_confidence():
    evidence = endpointslice_evidence(make_endpointslice(endpoint_count=0))
    hypotheses = ServiceSelectorMismatchRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.SERVICE_SELECTOR_MISMATCH
    assert hypotheses[0].confidence == Confidence.MEDIUM


def test_endpoints_present_but_not_ready_does_not_fire():
    # Pods matched the selector but aren't ready -- a Pod-level readiness problem,
    # not a Service misconfiguration. Must not be misdiagnosed as a selector mismatch.
    evidence = endpointslice_evidence(make_endpointslice(endpoint_count=2, ready_count=0))
    assert ServiceSelectorMismatchRule().evaluate(evidence) == []


def test_endpoints_present_and_ready_does_not_fire():
    evidence = endpointslice_evidence(make_endpointslice(endpoint_count=2, ready_count=2))
    assert ServiceSelectorMismatchRule().evaluate(evidence) == []


def test_zero_endpoints_is_a_failure_signal():
    evidence = endpointslice_evidence(make_endpointslice(endpoint_count=0))
    assert has_failure_signal(evidence) is True


def test_endpoints_present_is_not_a_failure_signal_even_if_unready():
    evidence = endpointslice_evidence(make_endpointslice(endpoint_count=2, ready_count=0))
    assert has_failure_signal(evidence) is False


def test_endpoints_present_is_confirmed_healthy():
    evidence = endpointslice_evidence(make_endpointslice(endpoint_count=1))
    assert is_confirmed_healthy(evidence) is True


def test_zero_endpoints_is_not_confirmed_healthy():
    evidence = endpointslice_evidence(make_endpointslice(endpoint_count=0))
    assert is_confirmed_healthy(evidence) is False


def test_resource_ref_from_service():
    ref = resource_ref_from_service(make_service(name="my-svc", namespace="ns", uid="abc"))
    assert ref.kind == "Service"
    assert ref.name == "my-svc"
    assert ref.uid == "abc"


def test_endpointslice_evidence_counts_ready_and_total():
    evidence = endpointslice_evidence(make_endpointslice(endpoint_count=3, ready_count=1))
    assert len(evidence) == 1
    assert evidence[0].data == {"total_endpoints": 3, "ready_endpoints": 1}

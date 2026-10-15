from kubesentinel.diagnostics.engine import DiagnosticEngine
from kubesentinel.diagnostics.rules.crash_loop import CrashLoopBackOffRule
from kubesentinel.diagnostics.rules.image_pull import ImagePullRule
from kubesentinel.diagnostics.rules.oomkilled import OOMKilledRule
from kubesentinel.models.diagnosis import Confidence, ReasonCode
from kubesentinel.observer.inspector import pod_evidence
from tests.unit.factories import make_pod, running_container_status, waiting_container_status


def test_oom_killed_rule_fires_on_last_terminated_oom():
    cs = waiting_container_status(
        reason="CrashLoopBackOff", last_terminated_reason="OOMKilled", last_exit_code=137
    )
    evidence = pod_evidence(make_pod(container_statuses=[cs]))

    hypotheses = OOMKilledRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.OOM_KILLED
    assert hypotheses[0].confidence == Confidence.HIGH
    assert hypotheses[0].auto_remediation_permitted is False


def test_oom_killed_rule_does_not_fire_without_oom_evidence():
    evidence = pod_evidence(make_pod(container_statuses=[running_container_status()]))
    assert OOMKilledRule().evaluate(evidence) == []


def test_oom_killed_rule_infers_from_sigkill_exit_code_when_reason_not_reported():
    # Some kubelet/runtime combinations report exit_code=137 under the generic reason
    # "Error" instead of "OOMKilled" -- this must still be caught, but only as a lower
    # confidence, explicitly inferred hypothesis.
    cs = waiting_container_status(
        reason="CrashLoopBackOff", restart_count=3, last_terminated_reason="Error", last_exit_code=137
    )
    evidence = pod_evidence(make_pod(container_statuses=[cs]))

    hypotheses = OOMKilledRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.OOM_KILLED
    assert hypotheses[0].confidence == Confidence.MEDIUM
    assert "INFERRED" in hypotheses[0].explanation


def test_oom_killed_rule_does_not_infer_from_single_sigkill():
    # A lone exit_code=137 with no restart history isn't enough signal to infer OOM.
    cs = waiting_container_status(
        reason="CrashLoopBackOff", restart_count=1, last_terminated_reason="Error", last_exit_code=137
    )
    evidence = pod_evidence(make_pod(container_statuses=[cs]))
    assert OOMKilledRule().evaluate(evidence) == []


def test_oom_killed_rule_does_not_infer_from_non_sigkill_exit_code():
    cs = waiting_container_status(
        reason="CrashLoopBackOff", restart_count=5, last_terminated_reason="Error", last_exit_code=1
    )
    evidence = pod_evidence(make_pod(container_statuses=[cs]))
    assert OOMKilledRule().evaluate(evidence) == []


def test_image_pull_backoff_rule_classifies_not_found():
    cs = waiting_container_status(
        reason="ImagePullBackOff", message="manifest unknown: the image does not exist"
    )
    evidence = pod_evidence(make_pod(phase="Pending", container_statuses=[cs]))

    hypotheses = ImagePullRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.IMAGE_PULL_BACKOFF
    assert "does not exist" in hypotheses[0].explanation


def test_err_image_pull_distinct_reason_code():
    cs = waiting_container_status(reason="ErrImagePull", message="unauthorized: denied")
    evidence = pod_evidence(make_pod(phase="Pending", container_statuses=[cs]))

    hypotheses = ImagePullRule().evaluate(evidence)
    assert hypotheses[0].reason_code == ReasonCode.ERR_IMAGE_PULL
    assert "credentials" in hypotheses[0].explanation


def test_crash_loop_rule_fires_and_references_termination():
    cs = waiting_container_status(
        reason="CrashLoopBackOff", restart_count=6, last_terminated_reason="Error", last_exit_code=1
    )
    evidence = pod_evidence(make_pod(container_statuses=[cs]))

    hypotheses = CrashLoopBackOffRule().evaluate(evidence)
    assert len(hypotheses) == 1
    h = hypotheses[0]
    assert h.reason_code == ReasonCode.CRASH_LOOP_BACKOFF
    assert h.confidence == Confidence.HIGH  # restart_count >= 3
    assert len(h.supporting_evidence) == 2  # waiting evidence + termination evidence


def test_crash_loop_rule_lower_confidence_for_few_restarts():
    cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=1)
    evidence = pod_evidence(make_pod(container_statuses=[cs]))

    hypotheses = CrashLoopBackOffRule().evaluate(evidence)
    assert hypotheses[0].confidence == Confidence.MEDIUM


def test_crash_loop_rule_notes_missing_termination_evidence():
    cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=4)
    cs.last_state = None
    evidence = pod_evidence(make_pod(container_statuses=[cs]))

    hypotheses = CrashLoopBackOffRule().evaluate(evidence)
    assert any("termination reason" in m for m in hypotheses[0].missing_evidence)


def test_healthy_pod_produces_no_hypotheses():
    evidence = pod_evidence(make_pod(phase="Running", container_statuses=[running_container_status()]))
    result = DiagnosticEngine().diagnose(evidence)
    assert result.hypotheses == []
    assert result.is_inconclusive is True


def test_engine_ranks_oom_above_crash_loop_when_both_match():
    # OOMKilled is more specific than the generic CrashLoopBackOff symptom; with equal
    # confidence (both HIGH), OOM must be ranked first because it's evaluated first.
    cs = waiting_container_status(
        reason="CrashLoopBackOff", restart_count=6, last_terminated_reason="OOMKilled", last_exit_code=137
    )
    evidence = pod_evidence(make_pod(container_statuses=[cs]))

    result = DiagnosticEngine().diagnose(evidence)
    reason_codes = [h.reason_code for h in result.hypotheses]
    assert ReasonCode.OOM_KILLED in reason_codes
    assert ReasonCode.CRASH_LOOP_BACKOFF in reason_codes
    assert result.top_hypothesis.reason_code == ReasonCode.OOM_KILLED

from kubesentinel.diagnostics.engine import DiagnosticEngine
from kubesentinel.diagnostics.rules.crash_loop import CrashLoopBackOffRule
from kubesentinel.diagnostics.rules.image_pull import ImagePullRule
from kubesentinel.diagnostics.rules.missing_config import MissingConfigReferenceRule
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


# Regression tests for a real bug found live: `IncidentManager` accumulates
# every observation's evidence into one growing list (`incident.evidence`),
# never superseding an older observation of the same field -- deliberate,
# since some rules genuinely need that full history (e.g.
# UnsatisfiedReplicasRule's sustained-deficit timing). But a rule whose own
# question is "is this CURRENTLY true" must not scan that entire history for
# "any matching entry, ever" -- a container that moved on from one waiting
# reason to another (or recovered) kept matching its own stale, superseded
# evidence forever. Each test below concatenates two observations' worth of
# evidence for the SAME container (same `source_field`, exactly like
# `incident.evidence` after two real `observe()` calls), simulating the
# real transition this was found against.


def test_image_pull_rule_ignores_a_superseded_waiting_state():
    stale = pod_evidence(make_pod(container_statuses=[waiting_container_status(reason="ErrImagePull")]))
    current = pod_evidence(
        make_pod(container_statuses=[waiting_container_status(reason="CrashLoopBackOff")])
    )
    assert ImagePullRule().evaluate(stale + current) == []


def test_image_pull_rule_fires_once_not_twice_across_two_matching_observations():
    first = pod_evidence(make_pod(container_statuses=[waiting_container_status(reason="ImagePullBackOff")]))
    second = pod_evidence(make_pod(container_statuses=[waiting_container_status(reason="ImagePullBackOff")]))
    assert len(ImagePullRule().evaluate(first + second)) == 1


def test_missing_config_reference_rule_ignores_a_superseded_waiting_state():
    # Transitions to another *unhealthy* waiting reason, not to Running --
    # a transition to Running would resolve the incident after two confirmed-
    # healthy observations before ever reaching this rule again (healthy
    # observations never even get merged into `incident.evidence`; see
    # `IncidentManager.observe`), so it's not a reachable case to guard here.
    stale = pod_evidence(
        make_pod(container_statuses=[waiting_container_status(reason="CreateContainerConfigError")])
    )
    current = pod_evidence(
        make_pod(container_statuses=[waiting_container_status(reason="CrashLoopBackOff", restart_count=5)])
    )
    assert MissingConfigReferenceRule().evaluate(stale + current) == []


def test_crash_loop_rule_fires_once_not_twice_across_two_matching_observations():
    first = pod_evidence(
        make_pod(container_statuses=[waiting_container_status(reason="CrashLoopBackOff", restart_count=2)])
    )
    second = pod_evidence(
        make_pod(container_statuses=[waiting_container_status(reason="CrashLoopBackOff", restart_count=5)])
    )
    hypotheses = CrashLoopBackOffRule().evaluate(first + second)
    assert len(hypotheses) == 1
    assert "5 restarts" in hypotheses[0].explanation  # reflects the latest observation, not the first


def test_oom_killed_rule_ignores_a_superseded_termination():
    # The exact scenario this was found against: a container OOM-killed once,
    # then later crashing for a completely unrelated reason, must not stay
    # diagnosed OOM_KILLED forever.
    stale_oom = pod_evidence(
        make_pod(
            container_statuses=[
                waiting_container_status(
                    reason="CrashLoopBackOff", last_terminated_reason="OOMKilled", last_exit_code=137
                )
            ]
        )
    )
    unrelated_crash = pod_evidence(
        make_pod(
            container_statuses=[
                waiting_container_status(
                    reason="CrashLoopBackOff", restart_count=8, last_terminated_reason="Error", last_exit_code=1
                )
            ]
        )
    )
    assert OOMKilledRule().evaluate(stale_oom + unrelated_crash) == []


def test_oom_killed_rule_still_fires_across_repeated_confirmations():
    first = pod_evidence(
        make_pod(
            container_statuses=[
                waiting_container_status(
                    reason="CrashLoopBackOff", last_terminated_reason="OOMKilled", last_exit_code=137
                )
            ]
        )
    )
    second = pod_evidence(
        make_pod(
            container_statuses=[
                waiting_container_status(
                    reason="CrashLoopBackOff", last_terminated_reason="OOMKilled", last_exit_code=137
                )
            ]
        )
    )
    assert len(OOMKilledRule().evaluate(first + second)) == 1

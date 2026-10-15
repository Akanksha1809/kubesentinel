from kubesentinel.incidents.health import has_failure_signal, is_confirmed_healthy
from kubesentinel.observer.inspector import pod_evidence
from tests.unit.factories import (
    make_pod,
    mid_restart_terminated_container_status,
    running_container_status,
    waiting_container_status,
)


def test_running_container_has_no_failure_signal_and_is_confirmed_healthy():
    evidence = pod_evidence(make_pod(container_statuses=[running_container_status()]))
    assert has_failure_signal(evidence) is False
    assert is_confirmed_healthy(evidence) is True


def test_crash_loop_backoff_has_failure_signal_and_is_not_confirmed_healthy():
    evidence = pod_evidence(make_pod(container_statuses=[waiting_container_status()]))
    assert has_failure_signal(evidence) is True
    assert is_confirmed_healthy(evidence) is False


def test_oom_in_last_state_is_a_failure_signal_even_if_waiting_reason_is_crashloop():
    cs = waiting_container_status(
        reason="CrashLoopBackOff", last_terminated_reason="OOMKilled", last_exit_code=137
    )
    evidence = pod_evidence(make_pod(container_statuses=[cs]))
    assert has_failure_signal(evidence) is True


def test_err_image_pull_is_a_failure_signal():
    cs = waiting_container_status(reason="ErrImagePull")
    evidence = pod_evidence(make_pod(container_statuses=[cs]))
    assert has_failure_signal(evidence) is True


def test_mid_restart_terminated_state_has_no_specific_failure_signal_and_is_not_confirmed_healthy():
    # Regression test for a bug found live against a real kind cluster: the brief
    # window where a container's CURRENT state is terminated with a generic reason
    # (not yet relabeled "waiting: CrashLoopBackOff" by the kubelet) must not look
    # confirmed-healthy, or an open incident would be prematurely resolved and
    # immediately reopened on the next watch event. See health.py's module docstring.
    cs = mid_restart_terminated_container_status(reason="Error", exit_code=1)
    evidence = pod_evidence(make_pod(container_statuses=[cs]))
    assert has_failure_signal(evidence) is False  # "Error" isn't a known-bad reason on its own
    assert is_confirmed_healthy(evidence) is False  # but it must not look healthy either


def test_confirmed_healthy_requires_every_container_ready():
    healthy = running_container_status(name="app")
    unhealthy = mid_restart_terminated_container_status(name="sidecar")
    evidence = pod_evidence(make_pod(container_statuses=[healthy, unhealthy]))
    assert is_confirmed_healthy(evidence) is False


def test_confirmed_healthy_false_with_no_container_state_evidence():
    evidence = pod_evidence(make_pod(container_statuses=[]))
    assert is_confirmed_healthy(evidence) is False

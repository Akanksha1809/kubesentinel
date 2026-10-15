import tempfile
from pathlib import Path

import pytest

from kubesentinel.incidents.manager import IncidentManager
from kubesentinel.metrics.registry import incidents_active
from kubesentinel.models.incident import IncidentStatus, ResourceRef
from kubesentinel.observer.inspector import pod_evidence
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from tests.unit.factories import (
    make_pod,
    mid_restart_terminated_container_status,
    running_container_status,
    waiting_container_status,
)


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as d:
        repo = SQLiteIncidentRepository(str(Path(d) / "test.db"))
        yield IncidentManager(repo)
        repo.close()


def _ref(uid: str = "uid-1") -> ResourceRef:
    return ResourceRef(namespace="kubesentinel-lab", kind="Pod", name="demo", uid=uid)


def test_observe_unhealthy_opens_incident(manager):
    cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    evidence = pod_evidence(make_pod(container_statuses=[cs]))

    incident = manager.observe(_ref(), evidence)
    assert incident is not None
    assert incident.status == IncidentStatus.DIAGNOSED
    assert incident.category == "CRASH_LOOP_BACKOFF"


def test_observe_healthy_with_no_prior_incident_does_nothing(manager):
    evidence = pod_evidence(make_pod(container_statuses=[running_container_status()]))
    incident = manager.observe(_ref(), evidence)
    assert incident is None


def test_repeated_unhealthy_observation_deduplicates_into_same_incident(manager):
    cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    evidence = pod_evidence(make_pod(container_statuses=[cs]))

    first = manager.observe(_ref(), evidence)
    second = manager.observe(_ref(), evidence)

    assert first.incident_id == second.incident_id


def test_single_healthy_observation_does_not_resolve_incident(manager):
    # Debounce: a flaky workload can be briefly Ready between crashes, so one
    # confirmed-healthy observation alone must not resolve the incident.
    ref = _ref()
    bad_cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    manager.observe(ref, pod_evidence(make_pod(container_statuses=[bad_cs])))

    good_evidence = pod_evidence(make_pod(container_statuses=[running_container_status()]))
    still_open = manager.observe(ref, good_evidence)

    assert still_open.status != IncidentStatus.RESOLVED
    assert still_open.resolved_at is None
    assert still_open.consecutive_healthy_observations == 1


def test_two_consecutive_healthy_observations_resolve_incident(manager):
    ref = _ref()
    bad_cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    manager.observe(ref, pod_evidence(make_pod(container_statuses=[bad_cs])))

    good_evidence = pod_evidence(make_pod(container_statuses=[running_container_status()]))
    manager.observe(ref, good_evidence)
    resolved = manager.observe(ref, good_evidence)

    assert resolved.status == IncidentStatus.RESOLVED
    assert resolved.resolved_at is not None
    assert resolved.recovery_duration_seconds is not None
    assert resolved.recovery_duration_seconds >= 0


def test_relapse_between_healthy_observations_resets_the_debounce(manager):
    ref = _ref()
    bad_cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    manager.observe(ref, pod_evidence(make_pod(container_statuses=[bad_cs])))

    good_evidence = pod_evidence(make_pod(container_statuses=[running_container_status()]))
    manager.observe(ref, good_evidence)  # 1st healthy observation

    manager.observe(ref, pod_evidence(make_pod(container_statuses=[bad_cs])))  # relapse

    still_open = manager.observe(ref, good_evidence)  # back to healthy, but debounce reset
    assert still_open.status != IncidentStatus.RESOLVED
    assert still_open.consecutive_healthy_observations == 1


def test_delete_recreate_with_new_uid_opens_a_new_incident(manager):
    bad_cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    evidence = pod_evidence(make_pod(container_statuses=[bad_cs]))

    first = manager.observe(_ref(uid="uid-A"), evidence)
    second = manager.observe(_ref(uid="uid-B"), evidence)

    assert first.incident_id != second.incident_id


def test_mid_restart_terminated_observation_does_not_prematurely_resolve_incident(manager):
    # Regression test for a flapping bug found live: a container's CURRENT state
    # briefly shows a generic "Error" termination (not yet relabeled
    # "waiting: CrashLoopBackOff") between restarts. That ambiguous snapshot must
    # neither resolve the open incident nor erase its diagnosis.
    ref = _ref()
    bad_cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    opened = manager.observe(ref, pod_evidence(make_pod(container_statuses=[bad_cs])))

    ambiguous_cs = mid_restart_terminated_container_status(restart_count=6)
    still_open = manager.observe(ref, pod_evidence(make_pod(container_statuses=[ambiguous_cs])))

    assert still_open.incident_id == opened.incident_id
    assert still_open.status == IncidentStatus.DIAGNOSED
    assert still_open.category == "CRASH_LOOP_BACKOFF"


def test_resolved_incident_does_not_block_a_new_incident_for_same_uid(manager):
    ref = _ref()
    bad_cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    good_evidence = pod_evidence(make_pod(container_statuses=[running_container_status()]))

    first = manager.observe(ref, pod_evidence(make_pod(container_statuses=[bad_cs])))
    manager.observe(ref, good_evidence)
    resolved = manager.observe(ref, good_evidence)
    assert resolved.status == IncidentStatus.RESOLVED

    second = manager.observe(ref, pod_evidence(make_pod(container_statuses=[bad_cs])))
    assert second.incident_id != first.incident_id
    assert second.status == IncidentStatus.DIAGNOSED


def test_reconcile_metrics_sets_active_gauge_from_persisted_open_incidents(manager):
    # Regression test for a bug found live: `incidents_active` is only ever
    # inc()/dec()'d by _open_new/_resolve, so an incident that was already
    # open before the current process started (loaded from SQLite, never
    # passed through _open_new in this process) left the gauge reading 0
    # even though /incidents correctly showed it. Force a stale value first
    # to simulate that exact process-restart scenario.
    bad_cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    evidence = pod_evidence(make_pod(container_statuses=[bad_cs]))
    manager.observe(_ref("uid-reconcile-1"), evidence)
    manager.observe(_ref("uid-reconcile-2"), evidence)

    incidents_active.labels(category="CRASH_LOOP_BACKOFF").set(0)
    manager.reconcile_metrics()

    assert incidents_active.labels(category="CRASH_LOOP_BACKOFF")._value.get() == 2


def test_reconcile_metrics_excludes_resolved_incidents(manager):
    ref = _ref("uid-reconcile-resolved")
    bad_cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    good_evidence = pod_evidence(make_pod(container_statuses=[running_container_status()]))
    manager.observe(ref, pod_evidence(make_pod(container_statuses=[bad_cs])))
    manager.observe(ref, good_evidence)
    manager.observe(ref, good_evidence)  # second confirmation resolves it

    incidents_active.labels(category="CRASH_LOOP_BACKOFF").set(99)
    manager.reconcile_metrics()

    assert incidents_active.labels(category="CRASH_LOOP_BACKOFF")._value.get() == 0

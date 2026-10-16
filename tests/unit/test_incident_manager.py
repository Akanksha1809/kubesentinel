import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from kubesentinel.incidents.manager import IncidentManager
from kubesentinel.metrics.registry import incidents_active
from kubesentinel.models.optimization import (
    EvidenceQuality,
    RecommendationCategory,
    ResourceRecommendation,
    UsageSummary,
    WorkloadKind,
)
from kubesentinel.models.incident import IncidentStatus, ResourceRef
from kubesentinel.observer.inspector import pod_evidence
from kubesentinel.persistence.optimization_repo import SQLiteOptimizationRepository
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from tests.unit.factories import (
    make_pod,
    mid_restart_terminated_container_status,
    running_container_status,
    waiting_container_status,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as d:
        repo = SQLiteIncidentRepository(str(Path(d) / "test.db"))
        yield IncidentManager(repo)
        repo.close()


def _ref(uid: str = "uid-1") -> ResourceRef:
    return ResourceRef(namespace="kubesentinel-lab", kind="Pod", name="demo", uid=uid)


def _ref_with_owner(uid: str = "uid-1", owner_name: str = "demo-deployment") -> ResourceRef:
    return ResourceRef(
        namespace="kubesentinel-lab",
        kind="Pod",
        name="demo-pod",
        uid=uid,
        owner_kind="Deployment",
        owner_name=owner_name,
        owner_uid="owner-uid-1",
    )


def _oom_evidence():
    cs = waiting_container_status(reason="CrashLoopBackOff", last_terminated_reason="OOMKilled", last_exit_code=137)
    return pod_evidence(make_pod(container_statuses=[cs]))


def _recommendation(workload_name: str, category: RecommendationCategory) -> ResourceRecommendation:
    return ResourceRecommendation(
        recommendation_id=str(uuid4()),
        namespace="kubesentinel-lab",
        workload_kind=WorkloadKind.DEPLOYMENT,
        workload_name=workload_name,
        container_name="app",
        category=category,
        usage=UsageSummary(evidence_quality=EvidenceQuality.INSUFFICIENT, observed_at=NOW),
        reasoning="test reasoning",
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )


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


class TestRecommendationCrossLink:
    """Milestone 1's own "stabilize the core" audit, item 3: an OOM_KILLED
    hypothesis cross-links back to an already-persisted MEMORY/DATA_QUALITY
    optimization recommendation for the same workload."""

    @pytest.fixture
    def optimization_repo(self):
        with tempfile.TemporaryDirectory() as d:
            repo = SQLiteOptimizationRepository(str(Path(d) / "opt.db"))
            yield repo
            repo.close()

    @pytest.fixture
    def manager_with_optimization(self, optimization_repo):
        with tempfile.TemporaryDirectory() as d:
            repo = SQLiteIncidentRepository(str(Path(d) / "inc.db"))
            yield IncidentManager(repo, optimization_repo=optimization_repo), optimization_repo
            repo.close()

    def test_oom_incident_links_to_a_memory_recommendation(self, manager_with_optimization):
        manager, optimization_repo = manager_with_optimization
        recommendation = _recommendation("demo-deployment", RecommendationCategory.MEMORY)
        optimization_repo.save_recommendation(recommendation)

        incident = manager.observe(_ref_with_owner(), _oom_evidence())

        top = incident.latest_diagnosis.top_hypothesis
        assert top.reason_code.value == "OOM_KILLED"
        assert top.related_recommendation_ids == [recommendation.recommendation_id]

    def test_oom_incident_links_to_a_data_quality_recommendation(self, manager_with_optimization):
        manager, optimization_repo = manager_with_optimization
        recommendation = _recommendation("demo-deployment", RecommendationCategory.DATA_QUALITY)
        optimization_repo.save_recommendation(recommendation)

        incident = manager.observe(_ref_with_owner(), _oom_evidence())

        top = incident.latest_diagnosis.top_hypothesis
        assert top.related_recommendation_ids == [recommendation.recommendation_id]

    def test_no_matching_recommendation_leaves_the_list_empty(self, manager_with_optimization):
        manager, _optimization_repo = manager_with_optimization

        incident = manager.observe(_ref_with_owner(), _oom_evidence())

        assert incident.latest_diagnosis.top_hypothesis.related_recommendation_ids == []

    def test_non_oom_hypothesis_is_never_enriched_even_with_a_matching_recommendation(
        self, manager_with_optimization
    ):
        manager, optimization_repo = manager_with_optimization
        optimization_repo.save_recommendation(_recommendation("demo-deployment", RecommendationCategory.MEMORY))
        cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)  # no OOM at all

        incident = manager.observe(_ref_with_owner(), pod_evidence(make_pod(container_statuses=[cs])))

        top = incident.latest_diagnosis.top_hypothesis
        assert top.reason_code.value == "CRASH_LOOP_BACKOFF"
        assert top.related_recommendation_ids == []

    def test_no_optimization_repo_wired_up_is_a_safe_no_op(self, manager):
        # The default `manager` fixture passes no optimization_repo at all --
        # every existing caller that only wires up `repo` must behave
        # exactly as it did before this feature existed.
        incident = manager.observe(_ref_with_owner(), _oom_evidence())

        top = incident.latest_diagnosis.top_hypothesis
        assert top.reason_code.value == "OOM_KILLED"
        assert top.related_recommendation_ids == []

    def test_recommendation_for_a_different_workload_is_not_linked(self, manager_with_optimization):
        manager, optimization_repo = manager_with_optimization
        optimization_repo.save_recommendation(_recommendation("some-other-deployment", RecommendationCategory.MEMORY))

        incident = manager.observe(_ref_with_owner(), _oom_evidence())

        assert incident.latest_diagnosis.top_hypothesis.related_recommendation_ids == []

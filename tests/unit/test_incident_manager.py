import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from kubernetes.client.rest import ApiException

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
from kubesentinel.observer.inspector import deployment_evidence, pod_evidence, resource_ref_from_deployment
from kubesentinel.persistence.optimization_repo import SQLiteOptimizationRepository
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from tests.unit.factories import (
    make_deployment,
    make_pod,
    make_replica_set,
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


def test_category_change_while_open_rebalances_the_active_gauge(manager):
    # Regression test for a real gauge-drift bug (TODO.md's Milestone-1
    # known-limitations list, item 4): `_open_new` incremented whichever
    # category was current at open; `_resolve` decremented whichever
    # category was current at resolve. If diagnosis re-categorized an
    # incident while it stayed open, the two didn't match -- the original
    # category's slot got stuck "active" forever and the new category's
    # slot would later decrement without this incident ever having
    # incremented it.
    # `incidents_active` is a process-wide Prometheus gauge, not reset
    # between tests -- pin a known baseline first, the same pattern
    # `test_reconcile_metrics_sets_active_gauge_from_persisted_open_incidents`
    # above already uses, rather than assume a clean start.
    incidents_active.labels(category="CRASH_LOOP_BACKOFF").set(0)
    incidents_active.labels(category="OOM_KILLED").set(0)

    ref = _ref("uid-rebalance-1")
    crash_loop_cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    opened = manager.observe(ref, pod_evidence(make_pod(container_statuses=[crash_loop_cs])))
    assert opened.category == "CRASH_LOOP_BACKOFF"
    assert incidents_active.labels(category="CRASH_LOOP_BACKOFF")._value.get() == 1

    recategorized = manager.observe(ref, _oom_evidence())  # same Pod, OOM evidence now present too
    assert recategorized.incident_id == opened.incident_id
    assert recategorized.category == "OOM_KILLED"

    # The old category's slot must drop back to 0, not stay stuck at 1.
    assert incidents_active.labels(category="CRASH_LOOP_BACKOFF")._value.get() == 0
    # The new category's slot must show exactly the one incident now
    # credited to it, not have gone negative from an unmatched dec() later.
    assert incidents_active.labels(category="OOM_KILLED")._value.get() == 1
    assert recategorized.active_metric_category == "OOM_KILLED"


def test_resolving_after_a_category_change_decrements_the_category_it_is_actually_credited_under(manager):
    incidents_active.labels(category="CRASH_LOOP_BACKOFF").set(0)
    incidents_active.labels(category="OOM_KILLED").set(0)

    ref = _ref("uid-rebalance-2")
    crash_loop_cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    manager.observe(ref, pod_evidence(make_pod(container_statuses=[crash_loop_cs])))
    manager.observe(ref, _oom_evidence())  # re-categorized to OOM_KILLED while still open

    good_evidence = pod_evidence(make_pod(container_statuses=[running_container_status()]))
    manager.observe(ref, good_evidence)
    resolved = manager.observe(ref, good_evidence)  # second confirmation resolves it

    assert resolved.status == IncidentStatus.RESOLVED
    assert resolved.active_metric_category is None
    # Must decrement OOM_KILLED (what the gauge was actually credited under
    # after rebalancing) -- the pre-fix bug would have decremented
    # CRASH_LOOP_BACKOFF a second time instead (already 0 after rebalance,
    # so it would have gone negative) and left OOM_KILLED stuck at 1.
    assert incidents_active.labels(category="OOM_KILLED")._value.get() == 0
    assert incidents_active.labels(category="CRASH_LOOP_BACKOFF")._value.get() == 0


def test_reconcile_metrics_backfills_active_metric_category_for_pre_existing_incidents(manager_with_clients):
    # An incident persisted before `active_metric_category` existed loads
    # with it defaulting to None. `reconcile_metrics()` must backfill it to
    # match `category` (and persist that) before any later
    # `_update_existing`/`_resolve` call runs against it -- otherwise
    # `_rebalance_active_metric` would see `None != category` and wrongly
    # treat an unrelated later observation as a fresh category change.
    manager, repo, _clients = manager_with_clients
    ref = _ref("uid-backfill-1")
    bad_cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
    opened = manager.observe(ref, pod_evidence(make_pod(container_statuses=[bad_cs])))
    assert opened.active_metric_category == "CRASH_LOOP_BACKOFF"

    # Simulate a row persisted before this field existed.
    opened.active_metric_category = None
    repo.save(opened)

    incidents_active.labels(category="CRASH_LOOP_BACKOFF").set(0)
    manager.reconcile_metrics()

    backfilled = repo.get(opened.incident_id)
    assert backfilled.active_metric_category == "CRASH_LOOP_BACKOFF"
    assert incidents_active.labels(category="CRASH_LOOP_BACKOFF")._value.get() == 1


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


# -- Stale Pod-incident supersede sweep (docs/INCIDENT_RECONCILIATION_DESIGN.md) --

_DEPLOYMENT_UID = "deploy-uid-1"
_NEW_REPLICASET_UID = "rs-uid-new"
_OLD_REPLICASET_UID = "rs-uid-old"


@pytest.fixture
def manager_with_clients():
    with tempfile.TemporaryDirectory() as d:
        repo = SQLiteIncidentRepository(str(Path(d) / "test.db"))
        clients = MagicMock()
        yield IncidentManager(repo, clients=clients), repo, clients
        repo.close()


def _healthy_deployment_ref_and_evidence(desired: int = 1):
    deployment = make_deployment(
        name="demo-deployment",
        uid=_DEPLOYMENT_UID,
        desired_replicas=desired,
        ready_replicas=desired,
        available_replicas=desired,
        updated_replicas=desired,
    )
    return resource_ref_from_deployment(deployment), deployment_evidence(deployment)


def _stale_pod_incident_ref(
    pod_uid: str = "pod-uid-old", owner_uid: str = _OLD_REPLICASET_UID, pod_name: str = "demo-deployment-old-abcde"
) -> ResourceRef:
    return ResourceRef(
        namespace="kubesentinel-lab",
        kind="Pod",
        name=pod_name,
        uid=pod_uid,
        owner_kind="ReplicaSet",
        owner_name="demo-deployment-old",
        owner_uid=owner_uid,
    )


class TestSupersedeStalePodIncidents:
    def test_no_clients_wired_up_is_a_safe_no_op(self, manager):
        # The default `manager` fixture passes no `clients` at all -- every
        # existing caller that only wires up `repo` must behave exactly as
        # it did before this feature existed.
        incident = manager.observe(*_healthy_deployment_ref_and_evidence())
        assert incident is None  # a healthy Deployment with no prior incident opens nothing

    def test_no_candidates_makes_zero_api_calls(self, manager_with_clients):
        manager, _repo, clients = manager_with_clients
        manager.observe(*_healthy_deployment_ref_and_evidence())
        clients.apps.list_namespaced_replica_set.assert_not_called()
        clients.core.read_namespaced_pod.assert_not_called()

    def test_supersedes_a_stale_pod_incident_confirmed_gone(self, manager_with_clients):
        manager, repo, clients = manager_with_clients
        stale_ref = _stale_pod_incident_ref()
        cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
        stale = manager.observe(stale_ref, pod_evidence(make_pod(container_statuses=[cs])))
        assert stale.status == IncidentStatus.DIAGNOSED

        clients.apps.list_namespaced_replica_set.return_value.items = [
            make_replica_set(uid=_OLD_REPLICASET_UID, owner_uid=_DEPLOYMENT_UID, owner_name="demo-deployment")
        ]
        clients.core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")

        manager.observe(*_healthy_deployment_ref_and_evidence())

        superseded = repo.get(stale.incident_id)
        assert superseded.status == IncidentStatus.SUPERSEDED
        assert superseded.resolved_at is not None
        # Load-bearing: never fabricate a recovery that was never observed.
        assert superseded.recovery_duration_seconds is None
        assert superseded.audit_log[-1].event_type == "incident_superseded"
        assert superseded.audit_log[-1].details["replaced_by_deployment"] == "demo-deployment"

    def test_decrements_the_active_incidents_gauge(self, manager_with_clients):
        manager, repo, clients = manager_with_clients
        stale_ref = _stale_pod_incident_ref()
        cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
        stale = manager.observe(stale_ref, pod_evidence(make_pod(container_statuses=[cs])))
        incidents_active.labels(category=stale.category).set(1)

        clients.apps.list_namespaced_replica_set.return_value.items = [
            make_replica_set(uid=_OLD_REPLICASET_UID, owner_uid=_DEPLOYMENT_UID, owner_name="demo-deployment")
        ]
        clients.core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")
        manager.observe(*_healthy_deployment_ref_and_evidence())

        assert incidents_active.labels(category=stale.category)._value.get() == 0

    def test_does_not_supersede_when_replicaset_already_pruned(self, manager_with_clients):
        manager, repo, clients = manager_with_clients
        stale_ref = _stale_pod_incident_ref()
        cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
        stale = manager.observe(stale_ref, pod_evidence(make_pod(container_statuses=[cs])))

        clients.apps.list_namespaced_replica_set.return_value.items = []  # the old ReplicaSet is gone
        clients.core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")

        manager.observe(*_healthy_deployment_ref_and_evidence())

        still_open = repo.get(stale.incident_id)
        assert still_open.status != IncidentStatus.SUPERSEDED
        clients.core.read_namespaced_pod.assert_not_called()

    def test_does_not_supersede_when_replicaset_belongs_to_a_different_deployment(self, manager_with_clients):
        manager, repo, clients = manager_with_clients
        stale_ref = _stale_pod_incident_ref()
        cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
        stale = manager.observe(stale_ref, pod_evidence(make_pod(container_statuses=[cs])))

        clients.apps.list_namespaced_replica_set.return_value.items = [
            make_replica_set(uid=_OLD_REPLICASET_UID, owner_uid="some-other-deployment-uid", owner_name="other")
        ]
        clients.core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")

        manager.observe(*_healthy_deployment_ref_and_evidence())

        still_open = repo.get(stale.incident_id)
        assert still_open.status != IncidentStatus.SUPERSEDED
        clients.core.read_namespaced_pod.assert_not_called()

    def test_does_not_supersede_when_the_pod_still_exists(self, manager_with_clients):
        manager, repo, clients = manager_with_clients
        stale_ref = _stale_pod_incident_ref()
        cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
        stale = manager.observe(stale_ref, pod_evidence(make_pod(container_statuses=[cs])))

        clients.apps.list_namespaced_replica_set.return_value.items = [
            make_replica_set(uid=_OLD_REPLICASET_UID, owner_uid=_DEPLOYMENT_UID, owner_name="demo-deployment")
        ]
        clients.core.read_namespaced_pod.return_value = make_pod(container_statuses=[cs])  # still there

        manager.observe(*_healthy_deployment_ref_and_evidence())

        still_open = repo.get(stale.incident_id)
        assert still_open.status != IncidentStatus.SUPERSEDED

    def test_does_not_supersede_on_an_inconclusive_live_check(self, manager_with_clients):
        # A non-404 error (e.g. a transient 5xx) must never be treated as
        # confirmation the Pod is gone -- conservative by design.
        manager, repo, clients = manager_with_clients
        stale_ref = _stale_pod_incident_ref()
        cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
        stale = manager.observe(stale_ref, pod_evidence(make_pod(container_statuses=[cs])))

        clients.apps.list_namespaced_replica_set.return_value.items = [
            make_replica_set(uid=_OLD_REPLICASET_UID, owner_uid=_DEPLOYMENT_UID, owner_name="demo-deployment")
        ]
        clients.core.read_namespaced_pod.side_effect = ApiException(status=500, reason="Internal Error")

        manager.observe(*_healthy_deployment_ref_and_evidence())

        still_open = repo.get(stale.incident_id)
        assert still_open.status != IncidentStatus.SUPERSEDED

    def test_fires_even_when_the_deployment_itself_never_opens_an_incident(self, manager_with_clients):
        # The core design decision (docs/INCIDENT_RECONCILIATION_DESIGN.md
        # Section 3): a clean rollout that never produces its own
        # ROLLOUT_FAILED/UNSATISFIED_REPLICAS incident must still trigger
        # the sweep -- that's the common case, not the edge case.
        manager, repo, clients = manager_with_clients
        stale_ref = _stale_pod_incident_ref()
        cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
        stale = manager.observe(stale_ref, pod_evidence(make_pod(container_statuses=[cs])))

        clients.apps.list_namespaced_replica_set.return_value.items = [
            make_replica_set(uid=_OLD_REPLICASET_UID, owner_uid=_DEPLOYMENT_UID, owner_name="demo-deployment")
        ]
        clients.core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")

        deployment_ref, evidence = _healthy_deployment_ref_and_evidence()
        result = manager.observe(deployment_ref, evidence)
        assert result is None  # the Deployment itself never opens an incident

        superseded = repo.get(stale.incident_id)
        assert superseded.status == IncidentStatus.SUPERSEDED

    def test_already_resolved_or_superseded_incidents_are_never_reconsidered(self, manager_with_clients):
        manager, repo, clients = manager_with_clients
        stale_ref = _stale_pod_incident_ref()
        cs = waiting_container_status(reason="CrashLoopBackOff", restart_count=5)
        stale = manager.observe(stale_ref, pod_evidence(make_pod(container_statuses=[cs])))
        # Resolve it the normal way first.
        for _ in range(2):
            manager.observe(stale_ref, pod_evidence(make_pod(container_statuses=[running_container_status()])))
        resolved = repo.get(stale.incident_id)
        assert resolved.status == IncidentStatus.RESOLVED

        manager.observe(*_healthy_deployment_ref_and_evidence())

        clients.apps.list_namespaced_replica_set.assert_not_called()

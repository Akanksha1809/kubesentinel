import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client import (
    V1Container,
    V1ObjectMeta,
    V1PodCondition,
    V1PodSpec,
    V1ResourceRequirements,
    V2CrossVersionObjectReference,
    V2HorizontalPodAutoscaler,
    V2HorizontalPodAutoscalerSpec,
    V2HorizontalPodAutoscalerStatus,
)
from kubernetes.client.rest import ApiException

from kubesentinel.config import Settings
from kubesentinel.metrics.registry import (
    optimization_analysis_total,
    optimization_last_success_timestamp,
    optimization_metrics_data_available,
    optimization_recommendations_total,
)
from kubesentinel.models.incident import Incident, IncidentStatus, ResourceRef
from kubesentinel.models.optimization import RecommendationCategory, RecommendationStatus
from kubesentinel.optimization.service import (
    InvalidRecommendationStateError,
    OptimizationService,
    RecommendationNotFoundError,
)
from kubesentinel.persistence.optimization_repo import SQLiteOptimizationRepository
from tests.unit.factories import make_pod

NAMESPACE = "kubesentinel-lab"


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteOptimizationRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _items(objs):
    return SimpleNamespace(items=objs)


def _standalone_pod(name="standalone", uid="pod-uid-1", missing_resources=True):
    pod = make_pod(
        name=name,
        uid=uid,
        owner=None,
        conditions=[V1PodCondition(type="Ready", status="True")],
    )
    resources = None if missing_resources else V1ResourceRequirements(
        requests={"cpu": "100m", "memory": "128Mi"}, limits={"cpu": "200m", "memory": "256Mi"}
    )
    pod.spec = V1PodSpec(containers=[V1Container(name="app", resources=resources)])
    return pod


def _clients(pods=(), metrics_unavailable=True):
    clients = MagicMock()
    clients.core.list_namespaced_pod.return_value = _items(list(pods))
    clients.apps.list_namespaced_replica_set.return_value = _items([])
    clients.apps.list_namespaced_deployment.return_value = _items([])
    clients.apps.list_namespaced_stateful_set.return_value = _items([])
    clients.apps.list_namespaced_daemon_set.return_value = _items([])
    clients.batch.list_namespaced_job.return_value = _items([])
    if metrics_unavailable:
        clients.custom_objects.list_namespaced_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        clients.custom_objects.list_cluster_custom_object.side_effect = ApiException(status=404, reason="Not Found")
    else:
        clients.custom_objects.list_namespaced_custom_object.return_value = {"items": []}
        clients.custom_objects.list_cluster_custom_object.return_value = {"items": []}
    return clients


def _settings(**overrides) -> Settings:
    defaults = dict(optimization_recommendation_ttl_seconds=3600.0)
    defaults.update(overrides)
    return Settings(**defaults)


def _hpa(scale_target_kind="Pod", scale_target_name="standalone", min_replicas=1, max_replicas=5, current_replicas=1):
    return V2HorizontalPodAutoscaler(
        metadata=V1ObjectMeta(name="demo-hpa", namespace=NAMESPACE),
        spec=V2HorizontalPodAutoscalerSpec(
            scale_target_ref=V2CrossVersionObjectReference(kind=scale_target_kind, name=scale_target_name),
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            metrics=[],
        ),
        status=V2HorizontalPodAutoscalerStatus(
            current_replicas=current_replicas, desired_replicas=current_replicas, current_metrics=[], conditions=[]
        ),
    )


class TestAnalyze:
    def test_analyze_persists_workloads_and_recommendations(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())

        summary = service.analyze(namespaces=[NAMESPACE])

        assert summary.workload_count == 1
        assert summary.recommendation_count >= 1  # missing requests/limits -> DATA_QUALITY
        assert summary.metrics_available is False
        assert summary.warning is not None
        assert len(repo.list_workload_snapshots()) == 1
        assert len(repo.list_recommendations()) >= 1

    def test_analyze_without_metrics_server_still_reports_data_quality(self, repo):
        clients = _clients(pods=[_standalone_pod(missing_resources=True)])
        service = OptimizationService(clients, repo, _settings())

        service.analyze(namespaces=[NAMESPACE])

        dq_recs = repo.list_recommendations(category=RecommendationCategory.DATA_QUALITY.value)
        assert len(dq_recs) == 1

    def test_second_analyze_supersedes_prior_new_recommendation(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())

        service.analyze(namespaces=[NAMESPACE])
        first_recs = repo.list_recommendations(status=RecommendationStatus.NEW.value)
        assert len(first_recs) == 1
        first_id = first_recs[0].recommendation_id

        service.analyze(namespaces=[NAMESPACE])
        all_recs = repo.list_recommendations()

        # The old NEW recommendation is now EXPIRED, and exactly one fresh
        # NEW one exists -- never two live NEW rows for the same gap.
        superseded = next(r for r in all_recs if r.recommendation_id == first_id)
        assert superseded.status == RecommendationStatus.EXPIRED
        assert len([r for r in all_recs if r.status == RecommendationStatus.NEW]) == 1

    def test_second_analyze_never_touches_an_already_reviewed_recommendation(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())

        service.analyze(namespaces=[NAMESPACE])
        rec = repo.list_recommendations()[0]
        service.review(rec.recommendation_id, RecommendationStatus.APPROVED, reviewed_by="akanksha")

        service.analyze(namespaces=[NAMESPACE])

        reloaded = repo.get_recommendation(rec.recommendation_id)
        assert reloaded.status == RecommendationStatus.APPROVED  # untouched by the second analyze()


class TestGetAndList:
    def test_get_missing_recommendation_raises(self, repo):
        service = OptimizationService(_clients(), repo, _settings())
        with pytest.raises(RecommendationNotFoundError):
            service.get_recommendation("does-not-exist")

    def test_list_recommendations_filters_by_namespace_and_category(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())
        service.analyze(namespaces=[NAMESPACE])

        assert len(service.list_recommendations(namespace=NAMESPACE)) >= 1
        assert len(service.list_recommendations(namespace="other-ns")) == 0
        assert len(service.list_recommendations(category=RecommendationCategory.DATA_QUALITY.value)) >= 1

    def test_expired_recommendation_reads_as_expired_without_a_background_job(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings(optimization_recommendation_ttl_seconds=1.0))
        service.analyze(namespaces=[NAMESPACE])
        rec_id = repo.list_recommendations()[0].recommendation_id

        # Force the stored expires_at into the past without any background
        # job -- the service must compute EXPIRED at read time.
        stored = repo.get_recommendation(rec_id)
        stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        repo.save_recommendation(stored)

        effective = service.get_recommendation(rec_id)
        assert effective.status == RecommendationStatus.EXPIRED
        # The stored row itself is untouched -- EXPIRED is a read-time view.
        assert repo.get_recommendation(rec_id).status == RecommendationStatus.NEW

    def test_list_recommendations_status_filter_applies_to_effective_status(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())
        service.analyze(namespaces=[NAMESPACE])
        rec_id = repo.list_recommendations()[0].recommendation_id
        stored = repo.get_recommendation(rec_id)
        stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        repo.save_recommendation(stored)

        assert len(service.list_recommendations(status=RecommendationStatus.EXPIRED.value)) == 1
        assert len(service.list_recommendations(status=RecommendationStatus.NEW.value)) == 0


class TestReview:
    def test_review_transitions_new_to_approved(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())
        service.analyze(namespaces=[NAMESPACE])
        rec_id = repo.list_recommendations()[0].recommendation_id

        reviewed = service.review(rec_id, RecommendationStatus.APPROVED, reviewed_by="akanksha", notes="looks right")

        assert reviewed.status == RecommendationStatus.APPROVED
        assert reviewed.reviewed_by == "akanksha"
        assert reviewed.review_notes == "looks right"
        assert reviewed.reviewed_at is not None

    def test_review_rejects_already_decided_recommendation(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())
        service.analyze(namespaces=[NAMESPACE])
        rec_id = repo.list_recommendations()[0].recommendation_id
        service.review(rec_id, RecommendationStatus.REJECTED, reviewed_by="akanksha")

        with pytest.raises(InvalidRecommendationStateError):
            service.review(rec_id, RecommendationStatus.APPROVED, reviewed_by="akanksha")

    def test_review_rejects_an_expired_recommendation(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())
        service.analyze(namespaces=[NAMESPACE])
        rec_id = repo.list_recommendations()[0].recommendation_id
        stored = repo.get_recommendation(rec_id)
        stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        repo.save_recommendation(stored)

        with pytest.raises(InvalidRecommendationStateError):
            service.review(rec_id, RecommendationStatus.APPROVED, reviewed_by="akanksha")

    def test_review_with_invalid_decision_raises(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())
        service.analyze(namespaces=[NAMESPACE])
        rec_id = repo.list_recommendations()[0].recommendation_id

        with pytest.raises(InvalidRecommendationStateError):
            service.review(rec_id, RecommendationStatus.NEW, reviewed_by="akanksha")

    def test_review_missing_recommendation_raises(self, repo):
        service = OptimizationService(_clients(), repo, _settings())
        with pytest.raises(RecommendationNotFoundError):
            service.review("does-not-exist", RecommendationStatus.APPROVED, reviewed_by="akanksha")

    def test_review_from_reviewed_to_approved_is_allowed(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())
        service.analyze(namespaces=[NAMESPACE])
        rec_id = repo.list_recommendations()[0].recommendation_id

        service.review(rec_id, RecommendationStatus.REVIEWED, reviewed_by="akanksha")
        final = service.review(rec_id, RecommendationStatus.APPROVED, reviewed_by="akanksha")

        assert final.status == RecommendationStatus.APPROVED


class TestSummary:
    def test_summary_aggregates_by_category_and_status(self, repo):
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())
        service.analyze(namespaces=[NAMESPACE])

        summary = service.summary()

        assert summary["total_recommendations"] >= 1
        assert "DATA_QUALITY" in summary["by_category"]
        assert "NEW" in summary["by_status"]
        assert summary["latest_analysis"] is not None
        assert summary["latest_analysis"]["metrics_available"] is False

    def test_summary_with_no_analysis_yet_is_empty_not_an_error(self, repo):
        service = OptimizationService(_clients(), repo, _settings())
        summary = service.summary()

        assert summary["total_recommendations"] == 0
        assert summary["latest_analysis"] is None


def _open_incident(uid="pod-uid-1", incident_id="incident-1") -> Incident:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Incident(
        incident_id=incident_id,
        resource=ResourceRef(namespace=NAMESPACE, kind="Pod", name="standalone", uid=uid),
        status=IncidentStatus.OPEN,
        first_observed_at=now,
        last_observed_at=now,
    )


class TestIncidentIntegration:
    """Milestone 6 (extension): a currently-open incident on a workload's own
    UID or any of its current Pods' UIDs blocks a proposed number and links
    the recommendation back to that incident -- read-only lookups into the
    *existing* IncidentRepository, never a new parallel incident concept."""

    def test_no_incident_repo_wired_up_produces_no_active_incident_flag(self, repo):
        # Matches every other test in this file: OptimizationService with no
        # incident_repo at all must behave exactly as it did before this
        # milestone -- the parameter is additive, not required.
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())

        service.analyze(namespaces=[NAMESPACE])

        recs = repo.list_recommendations()
        assert recs
        assert all("ACTIVE_INCIDENT" not in r.safety_flags for r in recs)

    def test_open_incident_on_pod_uid_flags_and_links_recommendation(self, repo):
        clients = _clients(pods=[_standalone_pod(uid="pod-uid-1")])
        incident_repo = MagicMock()
        incident_repo.find_open_by_resource_uid.side_effect = (
            lambda uid: _open_incident(uid="pod-uid-1") if uid == "pod-uid-1" else None
        )
        service = OptimizationService(clients, repo, _settings(), incident_repo=incident_repo)

        service.analyze(namespaces=[NAMESPACE])

        recs = repo.list_recommendations()
        assert recs
        for r in recs:
            assert "ACTIVE_INCIDENT" in r.safety_flags
            assert r.related_incident_ids == ["incident-1"]
            assert r.proposed_requests is None
            assert r.proposed_limits is None

    def test_resolved_incident_does_not_count_as_active(self, repo):
        # find_open_by_resource_uid (the real SQLite implementation) never
        # returns a RESOLVED incident at all -- this pins that the service
        # trusts that contract rather than re-filtering by status itself.
        clients = _clients(pods=[_standalone_pod()])
        incident_repo = MagicMock()
        incident_repo.find_open_by_resource_uid.return_value = None
        service = OptimizationService(clients, repo, _settings(), incident_repo=incident_repo)

        service.analyze(namespaces=[NAMESPACE])

        recs = repo.list_recommendations()
        assert all("ACTIVE_INCIDENT" not in r.safety_flags for r in recs)

    def test_incident_lookup_checks_every_uid_exactly_once_each(self, repo):
        clients = _clients(pods=[_standalone_pod(uid="pod-uid-1")])
        incident_repo = MagicMock()
        incident_repo.find_open_by_resource_uid.return_value = None
        service = OptimizationService(clients, repo, _settings(), incident_repo=incident_repo)

        service.analyze(namespaces=[NAMESPACE])

        # A standalone Pod's workload_uid equals its own Pod UID, so the
        # single real UID here is checked exactly once, not once per
        # internal reference to it.
        checked_uids = [call.args[0] for call in incident_repo.find_open_by_resource_uid.call_args_list]
        assert checked_uids.count("pod-uid-1") == 1


class TestCapacityIntegration:
    """Milestone 5 (extension): HPA inventory is built and attached once per
    analyze() pass, and analyze_capacity() runs alongside analyze_workload()
    for every workload -- see docs/CAPACITY_PLANNING.md."""

    def test_hpa_targeting_a_workload_produces_a_capacity_recommendation(self, repo):
        clients = _clients(pods=[_standalone_pod(missing_resources=False)])
        clients.autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = _items([_hpa()])
        service = OptimizationService(clients, repo, _settings())

        service.analyze(namespaces=[NAMESPACE])

        capacity_recs = repo.list_recommendations(category=RecommendationCategory.CAPACITY.value)
        assert len(capacity_recs) == 1
        assert "demo-hpa" in capacity_recs[0].reasoning

    def test_no_hpa_at_all_produces_no_capacity_recommendation(self, repo):
        clients = _clients(pods=[_standalone_pod(missing_resources=False)])
        service = OptimizationService(clients, repo, _settings())

        service.analyze(namespaces=[NAMESPACE])

        assert repo.list_recommendations(category=RecommendationCategory.CAPACITY.value) == []

    def test_capacity_recommendation_goes_through_the_same_safety_checks(self, repo):
        # An unhealthy workload must suppress a capacity finding's proposed
        # numbers the same generic way every other category already does --
        # confirming analyze_capacity()'s output is run through
        # safety.py::apply_safety_checks, not persisted directly.
        clients = _clients(pods=[_standalone_pod(missing_resources=False)])
        clients.autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = _items([_hpa()])
        incident_repo = MagicMock()
        incident_repo.find_open_by_resource_uid.return_value = _open_incident()
        service = OptimizationService(clients, repo, _settings(), incident_repo=incident_repo)

        service.analyze(namespaces=[NAMESPACE])

        capacity_recs = repo.list_recommendations(category=RecommendationCategory.CAPACITY.value)
        assert capacity_recs
        assert "ACTIVE_INCIDENT" in capacity_recs[0].safety_flags
        assert capacity_recs[0].related_incident_ids == ["incident-1"]

    def test_node_inventory_failure_degrades_without_aborting_analyze(self, repo):
        clients = _clients(pods=[_standalone_pod(missing_resources=False)])
        clients.autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = _items([_hpa()])
        clients.core.list_node.side_effect = ApiException(status=503, reason="unreachable")
        service = OptimizationService(clients, repo, _settings())

        summary = service.analyze(namespaces=[NAMESPACE])

        assert summary.workload_count == 1
        capacity_recs = repo.list_recommendations(category=RecommendationCategory.CAPACITY.value)
        assert len(capacity_recs) == 1  # the HPA status finding still happens; only the headroom check degrades


class TestOptimizationMetrics:
    """Milestone 6 (extension): bounded-cardinality Prometheus metrics for
    the optimization engine, matching the reset-then-assert pattern already
    used for `incidents_active` in test_incident_manager.py."""

    def _reset(self):
        optimization_analysis_total.labels(status="success")._value.set(0)
        optimization_analysis_total.labels(status="partial")._value.set(0)
        optimization_analysis_total.labels(status="failed")._value.set(0)
        optimization_recommendations_total.labels(category="DATA_QUALITY")._value.set(0)
        optimization_metrics_data_available.labels(provider="metrics_api").set(-1)
        optimization_metrics_data_available.labels(provider="prometheus").set(-1)
        optimization_last_success_timestamp.set(0)

    def test_analyze_without_metrics_records_partial_not_success(self, repo):
        self._reset()
        clients = _clients(pods=[_standalone_pod()], metrics_unavailable=True)
        service = OptimizationService(clients, repo, _settings())

        service.analyze(namespaces=[NAMESPACE])

        assert optimization_analysis_total.labels(status="partial")._value.get() == 1
        assert optimization_analysis_total.labels(status="success")._value.get() == 0
        assert optimization_metrics_data_available.labels(provider="metrics_api")._value.get() == 0

    def test_analyze_with_metrics_available_records_success(self, repo):
        self._reset()
        clients = _clients(pods=[_standalone_pod()], metrics_unavailable=False)
        service = OptimizationService(clients, repo, _settings())

        service.analyze(namespaces=[NAMESPACE])

        assert optimization_analysis_total.labels(status="success")._value.get() == 1
        assert optimization_metrics_data_available.labels(provider="metrics_api")._value.get() == 1

    def test_analyze_records_recommendation_count_by_category(self, repo):
        self._reset()
        clients = _clients(pods=[_standalone_pod(missing_resources=True)])
        service = OptimizationService(clients, repo, _settings())

        service.analyze(namespaces=[NAMESPACE])

        assert optimization_recommendations_total.labels(category="DATA_QUALITY")._value.get() >= 1

    def test_analyze_sets_last_success_timestamp_even_when_partial(self, repo):
        self._reset()
        clients = _clients(pods=[_standalone_pod()], metrics_unavailable=True)
        service = OptimizationService(clients, repo, _settings())

        service.analyze(namespaces=[NAMESPACE])

        assert optimization_last_success_timestamp._value.get() > 0

    def test_analyze_failure_records_failed_status_and_reraises(self, repo):
        self._reset()
        clients = _clients(pods=[_standalone_pod()])
        clients.core.list_namespaced_pod.side_effect = ApiException(status=500, reason="boom")
        service = OptimizationService(clients, repo, _settings())

        with pytest.raises(ApiException):
            service.analyze(namespaces=[NAMESPACE])

        assert optimization_analysis_total.labels(status="failed")._value.get() == 1
        assert optimization_analysis_total.labels(status="success")._value.get() == 0
        assert optimization_analysis_total.labels(status="partial")._value.get() == 0

    def test_prometheus_gauge_reflects_configuration_not_last_query(self, repo):
        self._reset()
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(
            clients, repo, _settings(optimization_prometheus_url="http://prom.example:9090")
        )

        service.analyze(namespaces=[NAMESPACE])

        assert optimization_metrics_data_available.labels(provider="prometheus")._value.get() == 1

    def test_prometheus_gauge_is_zero_when_not_configured(self, repo):
        self._reset()
        clients = _clients(pods=[_standalone_pod()])
        service = OptimizationService(clients, repo, _settings())

        service.analyze(namespaces=[NAMESPACE])

        assert optimization_metrics_data_available.labels(provider="prometheus")._value.get() == 0

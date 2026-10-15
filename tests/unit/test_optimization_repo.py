import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from kubesentinel.models.optimization import (
    AnalysisRunSummary,
    RecommendationCategory,
    RecommendationStatus,
    ResourceRecommendation,
    UsageSummary,
    WorkloadKind,
    WorkloadSnapshot,
    EvidenceQuality,
)
from kubesentinel.persistence.optimization_repo import SQLiteOptimizationRepository

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteOptimizationRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _recommendation(
    namespace: str = "kubesentinel-lab",
    workload_name: str = "demo",
    category: RecommendationCategory = RecommendationCategory.CPU,
    status: RecommendationStatus = RecommendationStatus.NEW,
) -> ResourceRecommendation:
    return ResourceRecommendation(
        recommendation_id=str(uuid4()),
        namespace=namespace,
        workload_kind=WorkloadKind.DEPLOYMENT,
        workload_name=workload_name,
        container_name="app",
        category=category,
        reasoning="test",
        usage=UsageSummary(evidence_quality=EvidenceQuality.INSUFFICIENT, observed_at=NOW),
        status=status,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def _workload(namespace: str = "kubesentinel-lab", workload_uid: str = "uid-1") -> WorkloadSnapshot:
    return WorkloadSnapshot(
        namespace=namespace,
        workload_kind=WorkloadKind.DEPLOYMENT,
        workload_name="demo",
        workload_uid=workload_uid,
    )


class TestRecommendations:
    def test_save_and_get_roundtrip(self, repo):
        rec = _recommendation()
        repo.save_recommendation(rec)

        loaded = repo.get_recommendation(rec.recommendation_id)
        assert loaded is not None
        assert loaded.recommendation_id == rec.recommendation_id
        assert loaded.namespace == "kubesentinel-lab"

    def test_get_missing_id_returns_none(self, repo):
        assert repo.get_recommendation("does-not-exist") is None

    def test_save_is_an_upsert_not_a_duplicate(self, repo):
        rec = _recommendation()
        repo.save_recommendation(rec)
        rec.status = RecommendationStatus.APPROVED
        repo.save_recommendation(rec)

        all_recs = repo.list_recommendations()
        assert len(all_recs) == 1
        assert all_recs[0].status == RecommendationStatus.APPROVED

    def test_list_filters_by_namespace(self, repo):
        repo.save_recommendation(_recommendation(namespace="kubesentinel-lab"))
        repo.save_recommendation(_recommendation(namespace="other-ns"))

        assert len(repo.list_recommendations(namespace="kubesentinel-lab")) == 1
        assert len(repo.list_recommendations(namespace="other-ns")) == 1
        assert len(repo.list_recommendations()) == 2

    def test_list_filters_by_workload_name(self, repo):
        repo.save_recommendation(_recommendation(workload_name="demo-a"))
        repo.save_recommendation(_recommendation(workload_name="demo-b"))

        assert len(repo.list_recommendations(workload_name="demo-a")) == 1

    def test_list_filters_by_category(self, repo):
        repo.save_recommendation(_recommendation(category=RecommendationCategory.CPU))
        repo.save_recommendation(_recommendation(category=RecommendationCategory.MEMORY))

        assert len(repo.list_recommendations(category="CPU")) == 1

    def test_list_filters_by_status(self, repo):
        repo.save_recommendation(_recommendation(status=RecommendationStatus.NEW))
        repo.save_recommendation(_recommendation(status=RecommendationStatus.REJECTED))

        assert len(repo.list_recommendations(status="NEW")) == 1


class TestWorkloadSnapshots:
    def test_save_and_list_roundtrip(self, repo):
        repo.save_workload_snapshots([_workload(workload_uid="uid-1"), _workload(workload_uid="uid-2")])

        snapshots = repo.list_workload_snapshots()
        assert len(snapshots) == 2

    def test_save_replaces_the_entire_prior_set(self, repo):
        repo.save_workload_snapshots([_workload(workload_uid="uid-1")])
        repo.save_workload_snapshots([_workload(workload_uid="uid-2")])

        snapshots = repo.list_workload_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0].workload_uid == "uid-2"

    def test_list_filters_by_namespace(self, repo):
        repo.save_workload_snapshots(
            [_workload(namespace="kubesentinel-lab", workload_uid="uid-1"), _workload(namespace="other-ns", workload_uid="uid-2")]
        )

        assert len(repo.list_workload_snapshots(namespace="kubesentinel-lab")) == 1


class TestAnalysisRuns:
    def test_no_run_yet_returns_none(self, repo):
        assert repo.get_latest_analysis_run() is None

    def test_save_and_get_roundtrip(self, repo):
        summary = AnalysisRunSummary(
            analyzed_at=NOW, namespaces=["kubesentinel-lab"], workload_count=5, recommendation_count=2, metrics_available=True
        )
        repo.save_analysis_run(summary)

        loaded = repo.get_latest_analysis_run()
        assert loaded is not None
        assert loaded.workload_count == 5

    def test_save_replaces_the_previous_run_not_accumulates(self, repo):
        repo.save_analysis_run(
            AnalysisRunSummary(analyzed_at=NOW, workload_count=1, recommendation_count=0, metrics_available=True)
        )
        repo.save_analysis_run(
            AnalysisRunSummary(
                analyzed_at=NOW + timedelta(minutes=1), workload_count=2, recommendation_count=1, metrics_available=False, warning="no metrics"
            )
        )

        loaded = repo.get_latest_analysis_run()
        assert loaded.workload_count == 2
        assert loaded.metrics_available is False
        assert loaded.warning == "no metrics"

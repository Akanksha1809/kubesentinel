import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from kubesentinel.models.network import DnsHealthResult, NetworkFinding, NetworkFindingClassification
from kubesentinel.persistence.network_repo import SQLiteNetworkRepository

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteNetworkRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _finding(namespace: str = "kubesentinel-lab", classification=NetworkFindingClassification.HEALTHY, **overrides) -> NetworkFinding:
    defaults = dict(
        finding_id=str(uuid4()),
        namespace=namespace,
        target_pod_name="demo-pod",
        analyzed_at=NOW,
        dns_health=DnsHealthResult(healthy=True, reasoning="ok"),
        classification=classification,
        reasoning="test reasoning",
        created_at=NOW,
    )
    defaults.update(overrides)
    return NetworkFinding(**defaults)


class TestSaveAndGet:
    def test_save_then_get_roundtrips(self, repo):
        finding = _finding()
        repo.save_finding(finding)

        fetched = repo.get_finding(finding.finding_id)

        assert fetched is not None
        assert fetched.finding_id == finding.finding_id
        assert fetched.namespace == finding.namespace

    def test_get_missing_id_returns_none(self, repo):
        assert repo.get_finding("does-not-exist") is None

    def test_save_is_an_upsert_not_a_duplicate_row(self, repo):
        finding = _finding()
        repo.save_finding(finding)
        finding.reasoning = "updated reasoning"
        repo.save_finding(finding)

        all_findings = repo.list_findings()

        assert len(all_findings) == 1
        assert all_findings[0].reasoning == "updated reasoning"


class TestListFindings:
    def test_filters_by_namespace(self, repo):
        repo.save_finding(_finding(namespace="ns-a"))
        repo.save_finding(_finding(namespace="ns-b"))

        results = repo.list_findings(namespace="ns-a")

        assert len(results) == 1
        assert results[0].namespace == "ns-a"

    def test_filters_by_classification(self, repo):
        repo.save_finding(_finding(classification=NetworkFindingClassification.HEALTHY))
        repo.save_finding(_finding(classification=NetworkFindingClassification.DNS_UNHEALTHY))

        healthy = repo.list_findings(classification=NetworkFindingClassification.HEALTHY.value)

        assert len(healthy) == 1
        assert healthy[0].classification == NetworkFindingClassification.HEALTHY

    def test_no_filters_returns_everything(self, repo):
        repo.save_finding(_finding())
        repo.save_finding(_finding())

        assert len(repo.list_findings()) == 2

    def test_empty_repo_returns_empty_list(self, repo):
        assert repo.list_findings() == []

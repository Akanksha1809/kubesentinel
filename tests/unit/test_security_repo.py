import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from kubesentinel.models.security import SecurityFinding, SecurityResourceRef, SecuritySeverity
from kubesentinel.persistence.security_repo import SQLiteSecurityRepository

NAMESPACE = "kubesentinel-lab"


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteSecurityRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _finding(namespace: str | None = NAMESPACE, severity=SecuritySeverity.HIGH, rule_id="PRIVILEGED_CONTAINER", **overrides) -> SecurityFinding:
    defaults = dict(
        finding_id=str(uuid4()),
        rule_id=rule_id,
        severity=severity,
        resource=SecurityResourceRef(namespace=namespace, kind="Pod", name="demo"),
        explanation="test explanation",
        remediation="test remediation",
    )
    defaults.update(overrides)
    return SecurityFinding(**defaults)


class TestSaveAndGet:
    def test_save_then_get_roundtrips(self, repo):
        finding = _finding()
        repo.save_findings([finding])

        fetched = repo.get_finding(finding.finding_id)

        assert fetched is not None
        assert fetched.finding_id == finding.finding_id
        assert fetched.resource.namespace == finding.resource.namespace

    def test_get_missing_id_returns_none(self, repo):
        assert repo.get_finding("does-not-exist") is None

    def test_save_findings_replaces_the_entire_previous_set(self, repo):
        # The real bug this pins: SecurityService.audit() re-evaluates the
        # whole cluster every call, so a second run's findings must
        # replace the first run's, never accumulate alongside them --
        # found live when a fixed false-positive rule still showed its
        # stale finding from an earlier run sitting in the table.
        repo.save_findings([_finding(rule_id="PRIVILEGED_CONTAINER")])
        repo.save_findings([_finding(rule_id="CLUSTER_ADMIN_BINDING")])

        all_findings = repo.list_findings()

        assert len(all_findings) == 1
        assert all_findings[0].rule_id == "CLUSTER_ADMIN_BINDING"

    def test_save_findings_with_empty_list_clears_everything(self, repo):
        repo.save_findings([_finding()])
        repo.save_findings([])

        assert repo.list_findings() == []

    def test_cluster_scoped_finding_roundtrips_with_null_namespace(self, repo):
        finding = _finding(namespace=None, resource=SecurityResourceRef(namespace=None, kind="ClusterRoleBinding", name="crb"))
        repo.save_findings([finding])

        fetched = repo.get_finding(finding.finding_id)

        assert fetched.resource.namespace is None


class TestListFindings:
    def test_filters_by_namespace(self, repo):
        repo.save_findings([_finding(namespace="ns-a"), _finding(namespace="ns-b")])

        results = repo.list_findings(namespace="ns-a")

        assert len(results) == 1
        assert results[0].resource.namespace == "ns-a"

    def test_filters_by_severity(self, repo):
        repo.save_findings([_finding(severity=SecuritySeverity.CRITICAL), _finding(severity=SecuritySeverity.LOW)])

        critical = repo.list_findings(severity=SecuritySeverity.CRITICAL.value)

        assert len(critical) == 1
        assert critical[0].severity == SecuritySeverity.CRITICAL

    def test_filters_by_rule_id(self, repo):
        repo.save_findings([_finding(rule_id="PRIVILEGED_CONTAINER"), _finding(rule_id="CLUSTER_ADMIN_BINDING")])

        results = repo.list_findings(rule_id="CLUSTER_ADMIN_BINDING")

        assert len(results) == 1
        assert results[0].rule_id == "CLUSTER_ADMIN_BINDING"

    def test_no_filters_returns_everything(self, repo):
        repo.save_findings([_finding(), _finding()])

        assert len(repo.list_findings()) == 2

    def test_empty_repo_returns_empty_list(self, repo):
        assert repo.list_findings() == []

    def test_cluster_scoped_findings_not_returned_by_namespace_filter(self, repo):
        repo.save_findings(
            [_finding(namespace=None, resource=SecurityResourceRef(namespace=None, kind="ClusterRoleBinding", name="crb"))]
        )

        assert repo.list_findings(namespace=NAMESPACE) == []
        assert len(repo.list_findings()) == 1

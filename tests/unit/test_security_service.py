import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client import V1Container, V1PodSpec, V1SecurityContext

from kubesentinel.config import Settings
from kubesentinel.metrics.registry import security_audit_total, security_findings_total
from kubesentinel.persistence.security_repo import SQLiteSecurityRepository
from kubesentinel.security.service import FindingNotFoundError, SecurityService
from tests.unit.factories import make_pod

NAMESPACE = "kubesentinel-lab"


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteSecurityRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _items(objs):
    return SimpleNamespace(items=objs)


def _privileged_pod():
    pod = make_pod(name="demo", namespace=NAMESPACE, owner=None)
    pod.spec = V1PodSpec(containers=[V1Container(name="app", security_context=V1SecurityContext(privileged=True))])
    return pod


def _clients(pods=()):
    clients = MagicMock()
    clients.core.list_namespaced_pod.return_value = _items(list(pods))
    clients.apps.list_namespaced_replica_set.return_value = _items([])
    clients.apps.list_namespaced_deployment.return_value = _items([])
    clients.apps.list_namespaced_stateful_set.return_value = _items([])
    clients.rbac_authorization.list_namespaced_role_binding.return_value = _items([])
    clients.rbac_authorization.list_namespaced_role.return_value = _items([])
    clients.rbac_authorization.list_cluster_role_binding.return_value = _items([])
    clients.rbac_authorization.list_cluster_role.return_value = _items([])
    clients.networking.list_namespaced_network_policy.return_value = _items([])
    clients.policy.list_namespaced_pod_disruption_budget.return_value = _items([])
    return clients


class TestAudit:
    def test_audit_persists_findings(self, repo):
        clients = _clients(pods=[_privileged_pod()])
        service = SecurityService(clients, repo, Settings())

        result = service.audit(namespaces=[NAMESPACE])

        assert any(f.rule_id == "PRIVILEGED_CONTAINER" for f in result.findings)
        assert len(repo.list_findings()) == len(result.findings)

    def test_second_audit_does_not_accumulate_stale_findings(self, repo):
        # The real bug this pins: SecurityService.audit() re-evaluates the
        # entire cluster every call; a second run (e.g. after the
        # offending Pod was fixed) must leave the repo showing only the
        # current state, never the first run's findings still sitting
        # alongside it.
        clients = _clients(pods=[_privileged_pod()])
        service = SecurityService(clients, repo, Settings())
        service.audit(namespaces=[NAMESPACE])

        fixed_clients = _clients(pods=[])
        service_after_fix = SecurityService(fixed_clients, repo, Settings())
        result = service_after_fix.audit(namespaces=[NAMESPACE])

        assert result.findings == []
        assert repo.list_findings() == []

    def test_audit_with_no_findings_succeeds(self, repo):
        clients = _clients(pods=[])
        service = SecurityService(clients, repo, Settings())

        result = service.audit(namespaces=[NAMESPACE])

        assert result.findings == []
        assert result.warnings == []

    def test_audit_failure_reraises(self, repo):
        clients = _clients()
        clients.core.list_namespaced_pod.side_effect = Exception("boom")
        service = SecurityService(clients, repo, Settings())

        with pytest.raises(Exception, match="boom"):
            service.audit(namespaces=[NAMESPACE])


class TestGetAndList:
    def test_get_missing_finding_raises(self, repo):
        service = SecurityService(_clients(), repo, Settings())
        with pytest.raises(FindingNotFoundError):
            service.get_finding("does-not-exist")

    def test_list_findings_after_audit(self, repo):
        clients = _clients(pods=[_privileged_pod()])
        service = SecurityService(clients, repo, Settings())

        service.audit(namespaces=[NAMESPACE])

        assert len(service.list_findings(namespace=NAMESPACE)) >= 1

    def test_list_findings_filters_by_rule_id(self, repo):
        clients = _clients(pods=[_privileged_pod()])
        service = SecurityService(clients, repo, Settings())
        service.audit(namespaces=[NAMESPACE])

        assert len(service.list_findings(rule_id="PRIVILEGED_CONTAINER")) == 1
        assert service.list_findings(rule_id="DOES_NOT_EXIST") == []


class TestSecurityMetrics:
    def _reset(self):
        security_audit_total.labels(result="success")._value.set(0)
        security_audit_total.labels(result="partial")._value.set(0)
        security_audit_total.labels(result="failed")._value.set(0)
        for severity in ("critical", "high", "medium", "low"):
            security_findings_total.labels(severity=severity)._value.set(0)

    def test_successful_audit_with_findings_records_success_and_severity(self, repo):
        self._reset()
        clients = _clients(pods=[_privileged_pod()])
        service = SecurityService(clients, repo, Settings())

        service.audit(namespaces=[NAMESPACE])

        assert security_audit_total.labels(result="success")._value.get() == 1
        assert security_findings_total.labels(severity="critical")._value.get() == 1

    def test_audit_failure_records_failed(self, repo):
        self._reset()
        clients = _clients()
        clients.core.list_namespaced_pod.side_effect = Exception("boom")
        service = SecurityService(clients, repo, Settings())

        with pytest.raises(Exception, match="boom"):
            service.audit(namespaces=[NAMESPACE])

        assert security_audit_total.labels(result="failed")._value.get() == 1
        assert security_audit_total.labels(result="success")._value.get() == 0

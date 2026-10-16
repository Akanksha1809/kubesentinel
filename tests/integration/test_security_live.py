"""Live kind-cluster scenario for the security & configuration auditor
(docs/SECURITY_AUDITING.md Section 11's "Live" tier). A real privileged
Pod, a real over-permissive ClusterRoleBinding, a real unrestricted-ingress
NetworkPolicy -- never mocked -- confirms the engine's own rule catalog
reaches the right conclusion against actual cluster state, that the
cluster's own built-in RBAC bootstrap identities are never flagged, and
that a full audit() pass never issues a mutating call against the
cluster.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kubesentinel.config import Settings
from kubesentinel.security.service import SecurityService
from kubesentinel.persistence.security_repo import SQLiteSecurityRepository
from tests.conftest import LAB_NAMESPACE

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")

_SCENARIO_DEPLOYMENT = "security-findings-demo"
_SCENARIO_SERVICE_ACCOUNT = "security-findings-demo-sa"


@pytest.fixture
def security_service(live_cluster_clients):
    with tempfile.TemporaryDirectory() as d:
        repo = SQLiteSecurityRepository(str(Path(d) / "test.db"))
        try:
            yield SecurityService(live_cluster_clients, repo, Settings())
        finally:
            repo.close()


def _require_scenario(live_cluster_clients):
    deployments = live_cluster_clients.apps.list_namespaced_deployment(namespace=LAB_NAMESPACE).items
    if not any(d.metadata.name == _SCENARIO_DEPLOYMENT for d in deployments):
        pytest.skip(f"'{_SCENARIO_DEPLOYMENT}' Deployment not found -- run ./lab/scripts/run-scenario.sh security-findings first")


def test_privileged_container_scenario_is_flagged(live_cluster_clients, security_service):
    _require_scenario(live_cluster_clients)

    result = security_service.audit(namespaces=[LAB_NAMESPACE])

    privileged_findings = [f for f in result.findings if f.rule_id == "PRIVILEGED_CONTAINER"]
    assert any(f.workload_name == _SCENARIO_DEPLOYMENT for f in privileged_findings)


def test_cluster_admin_binding_scenario_is_flagged_cluster_wide(live_cluster_clients, security_service):
    _require_scenario(live_cluster_clients)
    # Cluster-wide on purpose: RBAC is audited without a namespace filter,
    # since ClusterRole/ClusterRoleBinding are themselves cluster-scoped.
    result = security_service.audit(namespaces=[])

    cluster_admin_findings = [f for f in result.findings if f.rule_id == "CLUSTER_ADMIN_BINDING"]
    assert any(_SCENARIO_SERVICE_ACCOUNT in e for f in cluster_admin_findings for e in f.evidence)


def test_builtin_rbac_bootstrap_bindings_are_never_flagged(live_cluster_clients, security_service):
    _require_scenario(live_cluster_clients)

    result = security_service.audit(namespaces=[])

    cluster_admin_findings = [f for f in result.findings if f.rule_id == "CLUSTER_ADMIN_BINDING"]
    assert not any("system:masters" in e for f in cluster_admin_findings for e in f.evidence)
    assert not any("kubeadm:cluster-admins" in e for f in cluster_admin_findings for e in f.evidence)


def test_unrestricted_ingress_policy_scenario_is_flagged(live_cluster_clients, security_service):
    _require_scenario(live_cluster_clients)

    result = security_service.audit(namespaces=[LAB_NAMESPACE])

    assert any(f.rule_id == "UNRESTRICTED_INGRESS_POLICY" for f in result.findings)


def test_second_audit_does_not_accumulate_stale_findings(live_cluster_clients, security_service):
    _require_scenario(live_cluster_clients)

    first = security_service.audit(namespaces=[LAB_NAMESPACE])
    second = security_service.audit(namespaces=[LAB_NAMESPACE])

    assert len(security_service.list_findings()) == len(second.findings)
    assert len(first.findings) == len(second.findings)


def test_full_audit_pass_never_issues_a_non_get_http_request(live_cluster_clients, security_service):
    pool_manager = live_cluster_clients.core.api_client.rest_client.pool_manager
    original_request = pool_manager.request
    methods_seen: set[str] = set()

    def spy(method, url, *args, **kwargs):
        methods_seen.add(method)
        return original_request(method, url, *args, **kwargs)

    with patch.object(pool_manager, "request", side_effect=spy):
        security_service.audit(namespaces=[LAB_NAMESPACE])

    assert methods_seen, "expected at least one HTTP call to have been made"
    assert methods_seen <= {"GET"}, f"a full audit() pass issued non-GET HTTP method(s): {methods_seen}"

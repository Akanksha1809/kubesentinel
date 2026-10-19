import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client import V1Container, V1PodCondition, V1PodSpec, V1PodStatus, V1ResourceRequirements
from kubernetes.client.rest import ApiException

from kubesentinel.metrics.registry import scheduling_analysis_total
from kubesentinel.models.scheduling import ConstraintType
from kubesentinel.persistence.scheduling_repo import SQLiteSchedulingRepository
from kubesentinel.scheduling.service import FindingNotFoundError, PodNotFoundError, SchedulingService
from tests.unit.factories import make_node, make_pod

NAMESPACE = "kubesentinel-lab"


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteSchedulingRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _items(objs):
    return SimpleNamespace(items=objs)


def _pending_pod(name="pending-pod", cpu="100m", memory="64Mi"):
    pod = make_pod(name=name, namespace=NAMESPACE, phase="Pending")
    pod.spec = V1PodSpec(
        containers=[V1Container(name="app", resources=V1ResourceRequirements(requests={"cpu": cpu, "memory": memory}))]
    )
    pod.status = V1PodStatus(phase="Pending", conditions=[])
    return pod


def _clients(pod=None, nodes=(), replicasets=(), quotas=(), limit_ranges=(), pod_exists=True):
    clients = MagicMock()
    if pod_exists:
        clients.core.read_namespaced_pod.return_value = pod or _pending_pod()
    else:
        clients.core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")
    clients.core.list_node.return_value = _items(list(nodes))
    clients.core.list_pod_for_all_namespaces.return_value = _items([])
    clients.apps.list_namespaced_replica_set.return_value = _items(list(replicasets))
    clients.core.list_namespaced_resource_quota.return_value = _items(list(quotas))
    clients.core.list_namespaced_limit_range.return_value = _items(list(limit_ranges))
    return clients


class TestAnalyzePod:
    def test_schedulable_pod_persists_and_returns_finding(self, repo):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        service = SchedulingService(clients, repo)

        finding = service.analyze_pod(NAMESPACE, "pending-pod")

        assert finding.any_node_schedulable is True
        assert repo.get_finding(finding.finding_id) is not None

    def test_missing_pod_raises_pod_not_found(self, repo):
        clients = _clients(pod_exists=False)
        service = SchedulingService(clients, repo)

        with pytest.raises(PodNotFoundError):
            service.analyze_pod(NAMESPACE, "does-not-exist")

    def test_unschedulable_pod_is_persisted_too(self, repo):
        node = make_node(name="node-1", allocatable={"cpu": "10m", "memory": "10"})
        clients = _clients(nodes=[node])
        service = SchedulingService(clients, repo)

        finding = service.analyze_pod(NAMESPACE, "pending-pod")

        assert finding.any_node_schedulable is False
        assert repo.get_finding(finding.finding_id).any_node_schedulable is False

    def test_scheduler_message_extracted_from_pod_scheduled_condition(self, repo):
        pod = _pending_pod()
        pod.status = V1PodStatus(
            phase="Pending",
            conditions=[V1PodCondition(type="PodScheduled", status="False", reason="Unschedulable", message="no fit")],
        )
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(pod=pod, nodes=[node])
        service = SchedulingService(clients, repo)

        finding = service.analyze_pod(NAMESPACE, "pending-pod")

        assert finding.scheduler_message == "no fit"

    def test_quota_never_checked_for_a_real_pod(self, repo):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        service = SchedulingService(clients, repo)

        finding = service.analyze_pod(NAMESPACE, "pending-pod")

        assert ConstraintType.NAMESPACE_QUOTA not in finding.evaluated_constraints
        clients.core.list_namespaced_resource_quota.assert_not_called()

    def test_limit_range_never_checked_for_a_real_pod(self, repo):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        service = SchedulingService(clients, repo)

        finding = service.analyze_pod(NAMESPACE, "pending-pod")

        assert ConstraintType.LIMIT_RANGE not in finding.evaluated_constraints
        clients.core.list_namespaced_limit_range.assert_not_called()


class TestAnalyzeHypothetical:
    def test_hypothetical_spec_is_analyzed_and_persisted(self, repo):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        service = SchedulingService(clients, repo)
        raw_spec = {"containers": [{"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}

        finding = service.analyze_hypothetical(NAMESPACE, raw_spec)

        assert finding.pod_name is None
        assert finding.any_node_schedulable is True
        assert repo.get_finding(finding.finding_id) is not None

    def test_quota_is_checked_for_a_hypothetical_spec(self, repo):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        service = SchedulingService(clients, repo)
        raw_spec = {"containers": [{"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}

        service.analyze_hypothetical(NAMESPACE, raw_spec)

        clients.core.list_namespaced_resource_quota.assert_called_once()

    def test_limit_range_is_checked_for_a_hypothetical_spec(self, repo):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        service = SchedulingService(clients, repo)
        raw_spec = {"containers": [{"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}

        finding = service.analyze_hypothetical(NAMESPACE, raw_spec)

        clients.core.list_namespaced_limit_range.assert_called_once()
        assert ConstraintType.LIMIT_RANGE in finding.evaluated_constraints

    def test_limit_range_list_failure_degrades_rather_than_crashes(self, repo):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        clients.core.list_namespaced_limit_range.side_effect = ApiException(status=403, reason="Forbidden")
        service = SchedulingService(clients, repo)
        raw_spec = {"containers": [{"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}

        finding = service.analyze_hypothetical(NAMESPACE, raw_spec)

        assert finding.any_node_schedulable is True

    def test_limit_range_list_failure_is_surfaced_as_a_warning_not_silently_swallowed(self, repo):
        # The real gap this closed: before, this failure was only
        # logged, never told to the caller -- the finding would report
        # "no LimitRange in this namespace" (satisfied=True) when the
        # true state was "couldn't check."
        from kubesentinel.models.completeness import EvidenceCompleteness

        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        clients.core.list_namespaced_limit_range.side_effect = ApiException(status=403, reason="Forbidden")
        service = SchedulingService(clients, repo)
        raw_spec = {"containers": [{"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}

        finding = service.analyze_hypothetical(NAMESPACE, raw_spec)

        assert len(finding.warnings) == 1
        assert "LimitRange" in finding.warnings[0]
        assert finding.evidence_completeness == EvidenceCompleteness.DEGRADED

    def test_quota_list_failure_is_surfaced_as_a_warning_too(self, repo):
        from kubesentinel.models.completeness import EvidenceCompleteness

        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        clients.core.list_namespaced_resource_quota.side_effect = ApiException(status=403, reason="Forbidden")
        service = SchedulingService(clients, repo)
        raw_spec = {"containers": [{"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}

        finding = service.analyze_hypothetical(NAMESPACE, raw_spec)

        assert len(finding.warnings) == 1
        assert "ResourceQuota" in finding.warnings[0]
        assert finding.evidence_completeness == EvidenceCompleteness.DEGRADED

    def test_no_failures_means_complete_evidence(self, repo):
        from kubesentinel.models.completeness import EvidenceCompleteness

        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        service = SchedulingService(clients, repo)
        raw_spec = {"containers": [{"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}

        finding = service.analyze_hypothetical(NAMESPACE, raw_spec)

        assert finding.warnings == []
        assert finding.evidence_completeness == EvidenceCompleteness.COMPLETE


class TestGetAndList:
    def test_get_missing_finding_raises(self, repo):
        clients = _clients()
        service = SchedulingService(clients, repo)

        with pytest.raises(FindingNotFoundError):
            service.get_finding("does-not-exist")

    def test_list_findings_after_analyze(self, repo):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        service = SchedulingService(clients, repo)

        service.analyze_pod(NAMESPACE, "pending-pod")

        assert len(service.list_findings(namespace=NAMESPACE)) == 1


class TestMetrics:
    def _reset(self):
        scheduling_analysis_total.labels(result="schedulable")._value.set(0)
        scheduling_analysis_total.labels(result="unschedulable")._value.set(0)
        scheduling_analysis_total.labels(result="failed")._value.set(0)

    def test_schedulable_result_increments_schedulable_counter(self, repo):
        self._reset()
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        service = SchedulingService(clients, repo)

        service.analyze_pod(NAMESPACE, "pending-pod")

        assert scheduling_analysis_total.labels(result="schedulable")._value.get() == 1
        assert scheduling_analysis_total.labels(result="unschedulable")._value.get() == 0

    def test_failure_increments_failed_counter_and_reraises(self, repo):
        self._reset()
        clients = _clients(pod_exists=False)
        service = SchedulingService(clients, repo)

        with pytest.raises(PodNotFoundError):
            service.analyze_pod(NAMESPACE, "does-not-exist")

        assert scheduling_analysis_total.labels(result="failed")._value.get() == 1


class TestIncidentLinking:
    """Mirrors OptimizationService's own ACTIVE_INCIDENT lookup: an open
    incident can be keyed on the Pod's own UID or its owning workload's
    UID, and the service checks both -- see
    SchedulingService._open_incident_ids_for."""

    def test_no_incident_repo_wired_up_is_a_safe_no_op(self, repo):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        service = SchedulingService(clients, repo)

        finding = service.analyze_pod(NAMESPACE, "pending-pod")

        assert finding.related_incident_ids == []

    def test_open_incident_on_pod_uid_is_linked(self, repo):
        pod = _pending_pod()  # default uid is "uid-1234" (tests/unit/factories.py)
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(pod=pod, nodes=[node])
        incident_repo = MagicMock()
        incident_repo.find_open_by_resource_uid.side_effect = lambda uid: (
            SimpleNamespace(incident_id="incident-1") if uid == "uid-1234" else None
        )
        service = SchedulingService(clients, repo, incident_repo=incident_repo)

        finding = service.analyze_pod(NAMESPACE, "pending-pod")

        assert finding.related_incident_ids == ["incident-1"]

    def test_no_open_incident_leaves_related_incident_ids_empty(self, repo):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node])
        incident_repo = MagicMock()
        incident_repo.find_open_by_resource_uid.return_value = None
        service = SchedulingService(clients, repo, incident_repo=incident_repo)

        finding = service.analyze_pod(NAMESPACE, "pending-pod")

        assert finding.related_incident_ids == []

    def test_persisted_finding_keeps_the_incident_link(self, repo):
        pod = _pending_pod()
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(pod=pod, nodes=[node])
        incident_repo = MagicMock()
        incident_repo.find_open_by_resource_uid.side_effect = lambda uid: (
            SimpleNamespace(incident_id="incident-1") if uid == "uid-1234" else None
        )
        service = SchedulingService(clients, repo, incident_repo=incident_repo)

        finding = service.analyze_pod(NAMESPACE, "pending-pod")

        assert repo.get_finding(finding.finding_id).related_incident_ids == ["incident-1"]

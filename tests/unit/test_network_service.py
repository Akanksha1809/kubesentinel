import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import (
    V1Deployment,
    V1DeploymentSpec,
    V1DeploymentStatus,
    V1ObjectMeta,
    V1PodCondition,
    V1Service,
    V1ServicePort,
)
from kubernetes.client.rest import ApiException

from kubesentinel.config import Settings
from kubesentinel.metrics.registry import network_analysis_total, network_probe_total
from kubesentinel.models.network import NetworkFindingClassification
from kubesentinel.network.service import (
    FindingNotFoundError,
    NetworkService,
    PodNotFoundError,
    ProbeDeniedError,
    ProbeDisabledError,
    ProbeTargetError,
)
from kubesentinel.persistence.network_repo import SQLiteNetworkRepository
from tests.unit.factories import make_pod, make_service

NAMESPACE = "kubesentinel-lab"


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteNetworkRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _items(objs):
    return SimpleNamespace(items=objs)


class _FakeRawResponse:
    def __init__(self, items: list[dict]):
        self.data = json.dumps({"items": items}).encode()


def _target_pod(name="target", ready=True):
    pod = make_pod(name=name, namespace=NAMESPACE, conditions=[V1PodCondition(type="Ready", status="True" if ready else "False")])
    pod.metadata.labels = {"app": "target"}
    return pod


def _endpoint(ready=True):
    return {"conditions": {"ready": ready}}


def _endpointslice(service_name, ready_count=1):
    return {
        "metadata": {"labels": {"kubernetes.io/service-name": service_name}},
        "endpoints": [_endpoint(ready=True) for _ in range(ready_count)],
    }


def _clients(pod=None, pod_exists=True, services=(), dns_healthy=True):
    clients = MagicMock()
    if pod_exists:
        clients.core.read_namespaced_pod.return_value = pod or _target_pod()
    else:
        clients.core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")
    clients.apps.list_namespaced_replica_set.return_value = _items([])
    clients.core.list_namespaced_service.return_value = _items(list(services))
    clients.core.list_namespaced_pod.return_value = _items([pod or _target_pod()])
    clients.networking.list_namespaced_network_policy.return_value = _items([])

    def _list_endpoint_slices(*args, **kwargs):
        if kwargs.get("namespace") == "kube-system":
            return _FakeRawResponse([_endpointslice("kube-dns", ready_count=1)] if dns_healthy else [])
        return _FakeRawResponse([_endpointslice(svc.metadata.name, ready_count=1) for svc in services])

    clients.discovery.list_namespaced_endpoint_slice.side_effect = _list_endpoint_slices

    ready_replicas = 2 if dns_healthy else 0
    clients.apps.read_namespaced_deployment.return_value = V1Deployment(
        metadata=V1ObjectMeta(name="coredns", namespace="kube-system"),
        spec=V1DeploymentSpec(replicas=2, selector={}, template={}),
        status=V1DeploymentStatus(ready_replicas=ready_replicas),
    )
    clients.core.read_namespaced_service.return_value = V1Service(metadata=V1ObjectMeta(name="kube-dns"))
    return clients


class TestAnalyzePod:
    def test_persists_and_returns_finding(self, repo):
        svc = make_service(name="svc", selector={"app": "target"})
        clients = _clients(services=[svc])
        service = NetworkService(clients, repo, Settings())

        finding = service.analyze_pod(NAMESPACE, "target")

        assert finding.target_pod_name == "target"
        assert finding.classification == NetworkFindingClassification.HEALTHY
        assert repo.get_finding(finding.finding_id) is not None

    def test_missing_pod_raises_pod_not_found(self, repo):
        clients = _clients(pod_exists=False)
        service = NetworkService(clients, repo, Settings())

        with pytest.raises(PodNotFoundError):
            service.analyze_pod(NAMESPACE, "does-not-exist")

    def test_dns_unhealthy_classification(self, repo):
        clients = _clients(dns_healthy=False)
        service = NetworkService(clients, repo, Settings())

        finding = service.analyze_pod(NAMESPACE, "target")

        assert finding.classification == NetworkFindingClassification.DNS_UNHEALTHY


class TestGetAndList:
    def test_get_missing_finding_raises(self, repo):
        clients = _clients()
        service = NetworkService(clients, repo, Settings())

        with pytest.raises(FindingNotFoundError):
            service.get_finding("does-not-exist")

    def test_list_findings_after_analyze(self, repo):
        clients = _clients()
        service = NetworkService(clients, repo, Settings())

        service.analyze_pod(NAMESPACE, "target")

        assert len(service.list_findings(namespace=NAMESPACE)) == 1


class TestIncidentLinking:
    def test_no_incident_repo_wired_up_is_a_safe_no_op(self, repo):
        clients = _clients()
        service = NetworkService(clients, repo, Settings())

        finding = service.analyze_pod(NAMESPACE, "target")

        assert finding.related_incident_ids == []

    def test_open_incident_on_pod_uid_is_linked(self, repo):
        pod = _target_pod()  # default uid is "uid-1234" (tests/unit/factories.py)
        clients = _clients(pod=pod)
        incident_repo = MagicMock()
        incident_repo.find_open_by_resource_uid.side_effect = lambda uid: (
            SimpleNamespace(incident_id="incident-1") if uid == "uid-1234" else None
        )
        service = NetworkService(clients, repo, Settings(), incident_repo=incident_repo)

        finding = service.analyze_pod(NAMESPACE, "target")

        assert finding.related_incident_ids == ["incident-1"]


class TestMetrics:
    def _reset(self):
        for label in ("healthy", "configuration_error", "missing_or_unready_endpoints", "dns_unhealthy", "policy_may_restrict", "inconclusive", "failed"):
            network_analysis_total.labels(result=label)._value.set(0)

    def test_healthy_result_increments_healthy_counter(self, repo):
        self._reset()
        svc = make_service(name="svc", selector={"app": "target"})
        clients = _clients(services=[svc])
        service = NetworkService(clients, repo, Settings())

        finding = service.analyze_pod(NAMESPACE, "target")

        assert finding.classification == NetworkFindingClassification.HEALTHY
        assert network_analysis_total.labels(result="healthy")._value.get() == 1

    def test_failure_increments_failed_counter_and_reraises(self, repo):
        self._reset()
        clients = _clients(pod_exists=False)
        service = NetworkService(clients, repo, Settings())

        with pytest.raises(PodNotFoundError):
            service.analyze_pod(NAMESPACE, "does-not-exist")

        assert network_analysis_total.labels(result="failed")._value.get() == 1


def _probe_clients(service_obj=None):
    clients = MagicMock()
    clients.core.read_namespaced_service.return_value = service_obj or make_service(
        name="svc", ports=[V1ServicePort(port=80, target_port=8080)], cluster_ip="10.0.0.5"
    )
    clients.batch.list_namespaced_job.return_value = _items([])
    return clients


def _enabled_settings(**overrides) -> Settings:
    defaults = dict(network_probe_enabled=True, network_probe_allowed_namespaces=NAMESPACE)
    defaults.update(overrides)
    return Settings(**defaults)


class TestRunProbe:
    def _reset_metrics(self):
        for label in ("dns_ok_tcp_ok", "dns_ok_tcp_failed", "dns_failed", "timeout", "denied", "failed"):
            network_probe_total.labels(result=label)._value.set(0)

    def test_disabled_by_default_raises_and_touches_nothing(self, repo):
        clients = _probe_clients()
        service = NetworkService(clients, repo, Settings())

        with pytest.raises(ProbeDisabledError):
            service.run_probe(NAMESPACE, "svc", 80)

        clients.core.read_namespaced_service.assert_not_called()

    def test_namespace_not_allowed_raises(self, repo):
        clients = _probe_clients()
        settings = _enabled_settings(network_probe_allowed_namespaces="some-other-namespace")
        service = NetworkService(clients, repo, settings)

        with pytest.raises(ProbeDeniedError):
            service.run_probe(NAMESPACE, "svc", 80)

        clients.core.read_namespaced_service.assert_not_called()

    def test_service_not_found_raises_probe_target_error(self, repo):
        clients = _probe_clients()
        clients.core.read_namespaced_service.side_effect = ApiException(status=404, reason="Not Found")
        service = NetworkService(clients, repo, _enabled_settings())

        with pytest.raises(ProbeTargetError):
            service.run_probe(NAMESPACE, "does-not-exist", 80)

    def test_unresolvable_port_raises_probe_target_error(self, repo):
        clients = _probe_clients()
        service = NetworkService(clients, repo, _enabled_settings())

        with pytest.raises(ProbeTargetError):
            service.run_probe(NAMESPACE, "svc", 9999)

    def test_cooldown_active_raises_probe_denied(self, repo):
        clients = _probe_clients()
        settings = _enabled_settings(network_probe_cooldown_seconds=300)
        service = NetworkService(clients, repo, settings)
        with (
            patch("kubesentinel.network.service.probe_module.create_probe_job"),
            patch("kubesentinel.network.service.probe_module.wait_for_job_completion", return_value=True),
            patch("kubesentinel.network.service.probe_module.read_job_pod_log", return_value="KUBESENTINEL_PROBE_DNS_ATTEMPT\nKUBESENTINEL_PROBE_DNS_OK\n"),
            patch("kubesentinel.network.service.probe_module.delete_probe_job"),
        ):
            service.run_probe(NAMESPACE, "svc", 80)

            with pytest.raises(ProbeDeniedError):
                service.run_probe(NAMESPACE, "svc", 80)

    def test_concurrency_limit_raises_probe_denied(self, repo):
        clients = _probe_clients()
        clients.batch.list_namespaced_job.return_value = _items(
            [SimpleNamespace(status=SimpleNamespace(succeeded=0, failed=0))]
        )
        service = NetworkService(clients, repo, _enabled_settings(network_probe_max_concurrent_per_namespace=1))

        with pytest.raises(ProbeDeniedError):
            service.run_probe(NAMESPACE, "svc", 80)

    def test_successful_probe_creates_and_deletes_job_and_persists_result(self, repo):
        self._reset_metrics()
        clients = _probe_clients()
        service = NetworkService(clients, repo, _enabled_settings())

        with (
            patch("kubesentinel.network.service.probe_module.create_probe_job") as mock_create,
            patch("kubesentinel.network.service.probe_module.wait_for_job_completion", return_value=True),
            patch(
                "kubesentinel.network.service.probe_module.read_job_pod_log",
                return_value="KUBESENTINEL_PROBE_DNS_ATTEMPT\nKUBESENTINEL_PROBE_DNS_OK\nKUBESENTINEL_PROBE_TCP_ATTEMPT\nKUBESENTINEL_PROBE_TCP_OK\n",
            ),
            patch("kubesentinel.network.service.probe_module.delete_probe_job") as mock_delete,
        ):
            result = service.run_probe(NAMESPACE, "svc", 80)

        mock_create.assert_called_once()
        mock_delete.assert_called_once()
        assert result.dns_resolved is True
        assert result.tcp_connected is True
        assert result.succeeded is True
        assert repo.get_probe_result(result.probe_id) is not None
        assert network_probe_total.labels(result="dns_ok_tcp_ok")._value.get() == 1

    def test_read_namespaced_service_call_is_request_timeout_bounded(self, repo):
        """The same live-caught bug probe.py's own calls were fixed for
        (an unbounded call can block far longer than any configured
        timeout implies) applies equally to this service's own direct
        read_namespaced_service call."""
        clients = _probe_clients()
        settings = _enabled_settings(network_probe_api_timeout_seconds=7.5)
        service = NetworkService(clients, repo, settings)

        with (
            patch("kubesentinel.network.service.probe_module.create_probe_job"),
            patch("kubesentinel.network.service.probe_module.wait_for_job_completion", return_value=True),
            patch("kubesentinel.network.service.probe_module.read_job_pod_log", return_value=""),
            patch("kubesentinel.network.service.probe_module.delete_probe_job"),
        ):
            service.run_probe(NAMESPACE, "svc", 80)

        assert clients.core.read_namespaced_service.call_args.kwargs["_request_timeout"] == 7.5

    def test_cleanup_always_runs_even_on_timeout(self, repo):
        clients = _probe_clients()
        service = NetworkService(clients, repo, _enabled_settings())

        with (
            patch("kubesentinel.network.service.probe_module.create_probe_job"),
            patch("kubesentinel.network.service.probe_module.wait_for_job_completion", return_value=False),
            patch("kubesentinel.network.service.probe_module.read_job_pod_log", return_value=""),
            patch("kubesentinel.network.service.probe_module.delete_probe_job") as mock_delete,
        ):
            result = service.run_probe(NAMESPACE, "svc", 80)

        mock_delete.assert_called_once()
        assert result.error is not None
        assert result.dns_resolved is None

    def test_cleanup_runs_even_if_log_read_raises(self, repo):
        clients = _probe_clients()
        service = NetworkService(clients, repo, _enabled_settings())

        with (
            patch("kubesentinel.network.service.probe_module.create_probe_job"),
            patch("kubesentinel.network.service.probe_module.wait_for_job_completion", return_value=True),
            patch("kubesentinel.network.service.probe_module.read_job_pod_log", side_effect=RuntimeError("boom")),
            patch("kubesentinel.network.service.probe_module.delete_probe_job") as mock_delete,
        ):
            with pytest.raises(RuntimeError):
                service.run_probe(NAMESPACE, "svc", 80)

        mock_delete.assert_called_once()

    def test_headless_service_skips_tcp_check(self, repo):
        svc = make_service(name="svc", ports=[V1ServicePort(port=80, target_port=8080)], cluster_ip="None")
        clients = _probe_clients(service_obj=svc)
        service = NetworkService(clients, repo, _enabled_settings())

        with (
            patch("kubesentinel.network.service.probe_module.create_probe_job"),
            patch("kubesentinel.network.service.probe_module.wait_for_job_completion", return_value=True),
            patch(
                "kubesentinel.network.service.probe_module.read_job_pod_log",
                return_value="KUBESENTINEL_PROBE_DNS_ATTEMPT\nKUBESENTINEL_PROBE_DNS_OK\n",
            ),
            patch("kubesentinel.network.service.probe_module.delete_probe_job"),
        ):
            result = service.run_probe(NAMESPACE, "svc", 80)

        assert result.tcp_applicable is False
        assert result.tcp_connected is None
        assert result.succeeded is True

    def test_dns_failed_result_not_denied_and_persisted(self, repo):
        self._reset_metrics()
        clients = _probe_clients()
        service = NetworkService(clients, repo, _enabled_settings())

        with (
            patch("kubesentinel.network.service.probe_module.create_probe_job"),
            patch("kubesentinel.network.service.probe_module.wait_for_job_completion", return_value=True),
            patch(
                "kubesentinel.network.service.probe_module.read_job_pod_log",
                return_value="KUBESENTINEL_PROBE_DNS_ATTEMPT\n",
            ),
            patch("kubesentinel.network.service.probe_module.delete_probe_job"),
        ):
            result = service.run_probe(NAMESPACE, "svc", 80)

        assert result.dns_resolved is False
        assert result.succeeded is False
        assert network_probe_total.labels(result="dns_failed")._value.get() == 1

    def test_related_finding_id_is_carried_through(self, repo):
        clients = _probe_clients()
        service = NetworkService(clients, repo, _enabled_settings())

        with (
            patch("kubesentinel.network.service.probe_module.create_probe_job"),
            patch("kubesentinel.network.service.probe_module.wait_for_job_completion", return_value=True),
            patch("kubesentinel.network.service.probe_module.read_job_pod_log", return_value=""),
            patch("kubesentinel.network.service.probe_module.delete_probe_job"),
        ):
            result = service.run_probe(NAMESPACE, "svc", 80, related_finding_id="finding-123")

        assert result.related_finding_id == "finding-123"


class TestProbeResultGetAndList:
    def test_get_missing_probe_result_raises(self, repo):
        clients = _probe_clients()
        service = NetworkService(clients, repo, _enabled_settings())

        with pytest.raises(FindingNotFoundError):
            service.get_probe_result("does-not-exist")

    def test_list_probe_results_after_run(self, repo):
        clients = _probe_clients()
        service = NetworkService(clients, repo, _enabled_settings())

        with (
            patch("kubesentinel.network.service.probe_module.create_probe_job"),
            patch("kubesentinel.network.service.probe_module.wait_for_job_completion", return_value=True),
            patch("kubesentinel.network.service.probe_module.read_job_pod_log", return_value=""),
            patch("kubesentinel.network.service.probe_module.delete_probe_job"),
        ):
            service.run_probe(NAMESPACE, "svc", 80)

        assert len(service.list_probe_results(namespace=NAMESPACE)) == 1

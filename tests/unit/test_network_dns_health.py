import json
from unittest.mock import MagicMock

from kubernetes.client import V1Deployment, V1DeploymentSpec, V1DeploymentStatus, V1ObjectMeta, V1Service
from kubernetes.client.rest import ApiException

from kubesentinel.network.dns_health import check_dns_health


class _FakeRawResponse:
    def __init__(self, items: list[dict]):
        self.data = json.dumps({"items": items}).encode()


def _coredns_deployment(ready=2, desired=2):
    return V1Deployment(
        metadata=V1ObjectMeta(name="coredns", namespace="kube-system"),
        spec=V1DeploymentSpec(replicas=desired, selector={}, template={}),
        status=V1DeploymentStatus(ready_replicas=ready),
    )


def _endpointslice(ready_count=2):
    endpoints = [{"conditions": {"ready": True}} for _ in range(ready_count)]
    return {"endpoints": endpoints}


def _clients(deployment=None, deployment_error=None, service_exists=True, service_error=None, endpoint_ready_count=2):
    clients = MagicMock()
    if deployment_error:
        clients.apps.read_namespaced_deployment.side_effect = deployment_error
    else:
        clients.apps.read_namespaced_deployment.return_value = deployment or _coredns_deployment()

    if service_error:
        clients.core.read_namespaced_service.side_effect = service_error
    elif service_exists:
        clients.core.read_namespaced_service.return_value = V1Service(metadata=V1ObjectMeta(name="kube-dns"))
    else:
        clients.core.read_namespaced_service.side_effect = ApiException(status=404, reason="Not Found")

    clients.discovery.list_namespaced_endpoint_slice.return_value = _FakeRawResponse(
        [_endpointslice(ready_count=endpoint_ready_count)]
    )
    return clients


class TestDnsHealth:
    def test_healthy_when_everything_checks_out(self):
        clients = _clients(deployment=_coredns_deployment(ready=2, desired=2))

        result = check_dns_health(clients)

        assert result.healthy is True
        assert result.coredns_ready_replicas == 2
        assert result.kube_dns_endpoint_count == 2

    def test_unhealthy_when_coredns_has_zero_ready_replicas(self):
        # The omitempty case: a genuinely-zero ready_replicas is absent from
        # the real API response, which the generated client deserializes as
        # None -- normalized to 0 here, same as optimization/inventory.py
        # already does for the identical reason.
        clients = _clients(deployment=_coredns_deployment(ready=None, desired=2))

        result = check_dns_health(clients)

        assert result.healthy is False
        assert result.coredns_ready_replicas == 0
        assert "zero ready replicas" in result.reasoning

    def test_unhealthy_when_coredns_partially_ready(self):
        clients = _clients(deployment=_coredns_deployment(ready=1, desired=2))

        result = check_dns_health(clients)

        assert result.healthy is False
        assert "1/2" in result.reasoning

    def test_unhealthy_when_kube_dns_service_missing(self):
        clients = _clients(service_exists=False)

        result = check_dns_health(clients)

        assert result.healthy is False
        assert "not found" in result.reasoning

    def test_unhealthy_when_zero_ready_endpoints(self):
        clients = _clients(endpoint_ready_count=0)

        result = check_dns_health(clients)

        assert result.healthy is False
        assert "zero ready endpoints" in result.reasoning

    def test_unhealthy_when_deployment_unreadable(self):
        clients = _clients(deployment_error=ApiException(status=403, reason="Forbidden"))

        result = check_dns_health(clients)

        assert result.healthy is False
        assert "could not read" in result.reasoning

    def test_deployment_read_failure_sets_check_succeeded_false(self):
        # Distinct from a *confirmed* problem: healthy=False alone can't
        # tell a caller whether DNS is really down or this just couldn't
        # check -- check_succeeded is the field that does.
        clients = _clients(deployment_error=ApiException(status=403, reason="Forbidden"))

        result = check_dns_health(clients)

        assert result.check_succeeded is False

    def test_service_read_failure_sets_check_succeeded_false(self):
        clients = _clients(service_error=ApiException(status=403, reason="Forbidden"))

        result = check_dns_health(clients)

        assert result.check_succeeded is False

    def test_endpointslice_read_failure_sets_check_succeeded_false(self):
        clients = _clients()
        clients.discovery.list_namespaced_endpoint_slice.side_effect = ApiException(status=403, reason="Forbidden")

        result = check_dns_health(clients)

        assert result.check_succeeded is False

    def test_a_confirmed_problem_leaves_check_succeeded_true(self):
        # Zero ready endpoints is a real, confirmed finding -- every read
        # succeeded, the data itself just shows a real problem. Must not
        # be conflated with a read failure.
        clients = _clients(endpoint_ready_count=0)

        result = check_dns_health(clients)

        assert result.healthy is False
        assert result.check_succeeded is True

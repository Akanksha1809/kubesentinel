import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client import (
    V1Container,
    V1ContainerPort,
    V1LoadBalancerIngress,
    V1PodCondition,
    V1PodSpec,
    V1ServicePort,
)

from kubesentinel.network.service_inventory import build_service_match_results
from tests.unit.factories import make_endpointslice, make_pod, make_service

NAMESPACE = "kubesentinel-lab"


def _items(objs):
    return SimpleNamespace(items=objs)


def _ready_pod(name="app-1", labels=None, container_ports=None):
    pod = make_pod(name=name, namespace=NAMESPACE, conditions=[V1PodCondition(type="Ready", status="True")])
    pod.metadata.labels = labels if labels is not None else {"app": "test-service"}
    pod.spec = V1PodSpec(containers=[V1Container(name="app", ports=container_ports or [])])
    return pod


class _FakeRawResponse:
    def __init__(self, items: list[dict]):
        self.data = json.dumps({"items": items}).encode()


def _clients(services=(), pods=(), endpointslices=()):
    clients = MagicMock()
    clients.core.list_namespaced_service.return_value = _items(list(services))
    clients.core.list_namespaced_pod.return_value = _items(list(pods))
    clients.discovery.list_namespaced_endpoint_slice.return_value = _FakeRawResponse(list(endpointslices))
    return clients


class TestSelectorMatching:
    def test_matches_pods_with_matching_labels(self):
        svc = make_service(name="svc", selector={"app": "svc"})
        pod = _ready_pod(name="p1", labels={"app": "svc"})
        clients = _clients(services=[svc], pods=[pod])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].matched_pod_names == ["p1"]
        assert results[0].ready_pod_names == ["p1"]

    def test_does_not_match_pods_with_different_labels(self):
        svc = make_service(name="svc", selector={"app": "svc"})
        pod = _ready_pod(name="p1", labels={"app": "other"})
        clients = _clients(services=[svc], pods=[pod])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].matched_pod_names == []

    def test_empty_selector_matches_nothing(self):
        svc = make_service(name="svc", selector={})
        pod = _ready_pod(name="p1", labels={"app": "svc"})
        clients = _clients(services=[svc], pods=[pod])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].matched_pod_names == []

    def test_not_ready_pod_matched_but_not_ready(self):
        svc = make_service(name="svc", selector={"app": "svc"})
        pod = make_pod(name="p1", namespace=NAMESPACE, conditions=[V1PodCondition(type="Ready", status="False")])
        pod.metadata.labels = {"app": "svc"}
        pod.spec = V1PodSpec(containers=[V1Container(name="app")])
        clients = _clients(services=[svc], pods=[pod])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].matched_pod_names == ["p1"]
        assert results[0].ready_pod_names == []


class TestEndpointSliceConsistency:
    def test_consistent_when_counts_agree(self):
        svc = make_service(name="svc", selector={"app": "svc"})
        pod = _ready_pod(name="p1", labels={"app": "svc"})
        slice_dict = make_endpointslice(service_name="svc", namespace=NAMESPACE, endpoint_count=1, ready_count=1)
        clients = _clients(services=[svc], pods=[pod], endpointslices=[slice_dict])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].endpointslice_consistent is True
        assert results[0].endpointslice_ready_count == 1

    def test_inconsistent_when_counts_disagree(self):
        svc = make_service(name="svc", selector={"app": "svc"})
        pod = _ready_pod(name="p1", labels={"app": "svc"})
        # Real EndpointSlice shows 0 ready, but selector-match computed 1 ready --
        # a genuine, real inconsistency this check exists to catch.
        slice_dict = make_endpointslice(service_name="svc", namespace=NAMESPACE, endpoint_count=1, ready_count=0)
        clients = _clients(services=[svc], pods=[pod], endpointslices=[slice_dict])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].endpointslice_consistent is False

    def test_no_selector_is_always_consistent(self):
        svc = make_service(name="svc", selector={})
        clients = _clients(services=[svc], pods=[], endpointslices=[])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].endpointslice_consistent is True

    def test_slices_for_other_services_are_ignored(self):
        svc = make_service(name="svc", selector={"app": "svc"})
        pod = _ready_pod(name="p1", labels={"app": "svc"})
        other_slice = make_endpointslice(service_name="other-svc", namespace=NAMESPACE, endpoint_count=5, ready_count=5)
        clients = _clients(services=[svc], pods=[pod], endpointslices=[other_slice])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].endpointslice_endpoint_count == 0


class TestPortResolution:
    def test_numeric_target_port_declared_on_container(self):
        svc = make_service(
            name="svc", selector={"app": "svc"}, ports=[V1ServicePort(port=80, target_port=8080)]
        )
        pod = _ready_pod(name="p1", labels={"app": "svc"}, container_ports=[V1ContainerPort(container_port=8080)])
        clients = _clients(services=[svc], pods=[pod])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].port_results[0].matched is True
        assert results[0].port_results[0].matched_container_port == 8080

    def test_numeric_target_port_not_declared_still_structurally_valid(self):
        svc = make_service(name="svc", selector={"app": "svc"}, ports=[V1ServicePort(port=80, target_port=9999)])
        pod = _ready_pod(name="p1", labels={"app": "svc"}, container_ports=[])
        clients = _clients(services=[svc], pods=[pod])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].port_results[0].matched is True
        assert "numeric" in results[0].port_results[0].reasoning

    def test_named_target_port_resolves(self):
        svc = make_service(name="svc", selector={"app": "svc"}, ports=[V1ServicePort(port=80, target_port="http")])
        pod = _ready_pod(
            name="p1", labels={"app": "svc"}, container_ports=[V1ContainerPort(name="http", container_port=8080)]
        )
        clients = _clients(services=[svc], pods=[pod])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].port_results[0].matched is True
        assert results[0].port_results[0].matched_container_port == 8080

    def test_named_target_port_does_not_resolve(self):
        svc = make_service(name="svc", selector={"app": "svc"}, ports=[V1ServicePort(port=80, target_port="http")])
        pod = _ready_pod(
            name="p1", labels={"app": "svc"}, container_ports=[V1ContainerPort(name="other", container_port=8080)]
        )
        clients = _clients(services=[svc], pods=[pod])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].port_results[0].matched is False
        assert "does not match" in results[0].port_results[0].reasoning

    def test_no_matched_pods_named_port_cannot_resolve(self):
        svc = make_service(name="svc", selector={"app": "nonexistent"}, ports=[V1ServicePort(port=80, target_port="http")])
        clients = _clients(services=[svc], pods=[])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].port_results[0].matched is False


class TestServiceType:
    def test_defaults_to_cluster_ip_when_unset(self):
        svc = make_service(name="svc", selector={"app": "svc"})
        clients = _clients(services=[svc], pods=[])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].service_type == "ClusterIP"
        assert results[0].load_balancer_ingress == []

    def test_node_port_populated_from_service_port(self):
        svc = make_service(
            name="svc",
            selector={"app": "svc"},
            service_type="NodePort",
            ports=[V1ServicePort(port=80, target_port=8080, node_port=30080)],
        )
        clients = _clients(services=[svc], pods=[])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].service_type == "NodePort"
        assert results[0].port_results[0].node_port == 30080

    def test_cluster_ip_service_has_no_node_port(self):
        svc = make_service(name="svc", selector={"app": "svc"}, ports=[V1ServicePort(port=80, target_port=8080)])
        clients = _clients(services=[svc], pods=[])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].port_results[0].node_port is None

    def test_load_balancer_with_assigned_ingress(self):
        svc = make_service(
            name="svc",
            selector={"app": "svc"},
            service_type="LoadBalancer",
            load_balancer_ingress=[V1LoadBalancerIngress(ip="203.0.113.5")],
        )
        clients = _clients(services=[svc], pods=[])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].service_type == "LoadBalancer"
        assert results[0].load_balancer_ingress == ["203.0.113.5"]

    def test_load_balancer_prefers_hostname_over_ip_when_both_set(self):
        svc = make_service(
            name="svc",
            selector={"app": "svc"},
            service_type="LoadBalancer",
            load_balancer_ingress=[V1LoadBalancerIngress(ip="203.0.113.5", hostname="lb.example.com")],
        )
        clients = _clients(services=[svc], pods=[])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].load_balancer_ingress == ["lb.example.com"]

    def test_load_balancer_with_no_ingress_yet_is_pending(self):
        svc = make_service(name="svc", selector={"app": "svc"}, service_type="LoadBalancer")
        clients = _clients(services=[svc], pods=[])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].service_type == "LoadBalancer"
        assert results[0].load_balancer_ingress == []

    def test_node_port_type_never_reports_load_balancer_ingress(self):
        # A NodePort Service has no status.loadBalancer at all -- confirms
        # the LoadBalancer-only field doesn't leak a stale/irrelevant value
        # for a different Service type.
        svc = make_service(name="svc", selector={"app": "svc"}, service_type="NodePort")
        clients = _clients(services=[svc], pods=[])

        results = build_service_match_results(clients, NAMESPACE)

        assert results[0].load_balancer_ingress == []

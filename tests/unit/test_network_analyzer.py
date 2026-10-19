from kubesentinel.models.network import (
    DnsHealthResult,
    NetworkFindingClassification,
    NetworkPolicyRelevance,
    PortMatchResult,
    ServiceMatchResult,
)
from kubesentinel.network.analyzer import analyze_network

NAMESPACE = "kubesentinel-lab"
POD = "target-pod"


def _dns(healthy=True, reasoning="coredns healthy") -> DnsHealthResult:
    return DnsHealthResult(healthy=healthy, reasoning=reasoning)


def _service(
    name="svc",
    matched=None,
    ready=None,
    consistent=True,
    port_results=None,
    service_type="ClusterIP",
    load_balancer_ingress=None,
) -> ServiceMatchResult:
    return ServiceMatchResult(
        service_name=name,
        selector={"app": "svc"},
        matched_pod_names=matched if matched is not None else [POD],
        ready_pod_names=ready if ready is not None else [POD],
        endpointslice_consistent=consistent,
        service_type=service_type,
        load_balancer_ingress=load_balancer_ingress or [],
        port_results=port_results or [PortMatchResult(service_port=80, target_port="80", matched=True, reasoning="ok")],
    )


class TestClassificationPriority:
    def test_dns_unhealthy_wins_over_everything(self):
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(healthy=False, reasoning="coredns down"),
            service_results=[_service()],
            applicable_policies=[],
        )
        assert finding.classification == NetworkFindingClassification.DNS_UNHEALTHY

    def test_no_selecting_service_is_inconclusive(self):
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service(matched=[], ready=[])],
            applicable_policies=[],
        )
        assert finding.classification == NetworkFindingClassification.INCONCLUSIVE

    def test_unmatched_port_is_configuration_error(self):
        bad_port = PortMatchResult(service_port=80, target_port="http", matched=False, reasoning="named port missing")
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service(port_results=[bad_port])],
            applicable_policies=[],
        )
        assert finding.classification == NetworkFindingClassification.CONFIGURATION_ERROR

    def test_endpointslice_inconsistency_is_configuration_error(self):
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service(consistent=False)],
            applicable_policies=[],
        )
        assert finding.classification == NetworkFindingClassification.CONFIGURATION_ERROR

    def test_matched_but_not_ready_is_missing_or_unready_endpoints(self):
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service(matched=[POD], ready=[])],
            applicable_policies=[],
        )
        assert finding.classification == NetworkFindingClassification.MISSING_OR_UNREADY_ENDPOINTS

    def test_applicable_policy_is_policy_may_restrict(self):
        policy = NetworkPolicyRelevance(policy_name="deny-all", applies_to_target=True, policy_types=["Ingress"], summary="denies all ingress")
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service()],
            applicable_policies=[policy],
        )
        assert finding.classification == NetworkFindingClassification.POLICY_MAY_RESTRICT

    def test_non_applicable_policy_does_not_trigger_policy_may_restrict(self):
        policy = NetworkPolicyRelevance(policy_name="other", applies_to_target=False, policy_types=["Ingress"], summary="irrelevant")
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service()],
            applicable_policies=[policy],
        )
        assert finding.classification == NetworkFindingClassification.HEALTHY

    def test_everything_fine_is_healthy(self):
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service()],
            applicable_policies=[],
        )
        assert finding.classification == NetworkFindingClassification.HEALTHY

    def test_config_error_takes_priority_over_policy(self):
        bad_port = PortMatchResult(service_port=80, target_port="http", matched=False, reasoning="named port missing")
        policy = NetworkPolicyRelevance(policy_name="deny-all", applies_to_target=True, policy_types=["Ingress"], summary="denies all ingress")
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service(port_results=[bad_port])],
            applicable_policies=[policy],
        )
        assert finding.classification == NetworkFindingClassification.CONFIGURATION_ERROR


class TestReasoningAndShape:
    def test_reasoning_always_includes_the_not_a_connectivity_proof_caveat(self):
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service()],
            applicable_policies=[],
        )
        assert "not a confirmed end-to-end connectivity test" in finding.reasoning

    def test_determinism(self):
        kwargs = dict(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service()],
            applicable_policies=[],
        )
        f1 = analyze_network(**kwargs)
        f2 = analyze_network(**kwargs)
        assert f1.classification == f2.classification
        assert f1.reasoning == f2.reasoning

    def test_workload_fields_carried_through(self):
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service()],
            applicable_policies=[],
            workload_kind="Deployment",
            workload_name="my-app",
        )
        assert finding.workload_kind == "Deployment"
        assert finding.workload_name == "my-app"


class TestLoadBalancerPendingIsInformationalOnly:
    def test_pending_load_balancer_noted_in_reasoning_but_stays_healthy(self):
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service(service_type="LoadBalancer", load_balancer_ingress=[])],
            applicable_policies=[],
        )
        assert finding.classification == NetworkFindingClassification.HEALTHY
        assert "LoadBalancer but has no assigned external" in finding.reasoning

    def test_assigned_load_balancer_ingress_produces_no_pending_note(self):
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service(service_type="LoadBalancer", load_balancer_ingress=["lb.example.com"])],
            applicable_policies=[],
        )
        assert finding.classification == NetworkFindingClassification.HEALTHY
        assert "has no assigned external" not in finding.reasoning

    def test_cluster_ip_service_never_gets_a_pending_note(self):
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[_service(service_type="ClusterIP")],
            applicable_policies=[],
        )
        assert "LoadBalancer" not in finding.reasoning

    def test_pending_load_balancer_noted_even_when_classification_is_configuration_error(self):
        # The informational note must survive alongside every other
        # classification branch, not just the HEALTHY happy path --
        # confirmed here against a Service that also fails port
        # resolution (a real, unrelated CONFIGURATION_ERROR).
        bad_port = PortMatchResult(service_port=80, target_port="http", matched=False, reasoning="does not match")
        finding = analyze_network(
            namespace=NAMESPACE,
            target_pod_name=POD,
            dns_health=_dns(),
            service_results=[
                _service(service_type="LoadBalancer", load_balancer_ingress=[], port_results=[bad_port])
            ],
            applicable_policies=[],
        )
        assert finding.classification == NetworkFindingClassification.CONFIGURATION_ERROR
        assert "LoadBalancer but has no assigned external" in finding.reasoning

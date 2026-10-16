from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client import (
    V1LabelSelector,
    V1LabelSelectorRequirement,
    V1NetworkPolicy,
    V1NetworkPolicyIngressRule,
    V1NetworkPolicySpec,
    V1ObjectMeta,
)

from kubesentinel.network.policy_inventory import build_applicable_policies
from tests.unit.factories import make_pod

NAMESPACE = "kubesentinel-lab"


def _items(objs):
    return SimpleNamespace(items=objs)


def _target_pod(labels=None):
    pod = make_pod(name="target", namespace=NAMESPACE)
    pod.metadata.labels = labels if labels is not None else {"app": "target"}
    return pod


def _policy(name="policy", pod_selector=None, policy_types=None, ingress=None, egress=None):
    return V1NetworkPolicy(
        metadata=V1ObjectMeta(name=name, namespace=NAMESPACE),
        spec=V1NetworkPolicySpec(
            pod_selector=pod_selector if pod_selector is not None else V1LabelSelector(),
            policy_types=policy_types,
            ingress=ingress,
            egress=egress,
        ),
    )


def _clients(policies=()):
    clients = MagicMock()
    clients.networking.list_namespaced_network_policy.return_value = _items(list(policies))
    return clients


class TestPodSelectorMatching:
    def test_empty_selector_matches_every_pod(self):
        policy = _policy(pod_selector=V1LabelSelector())
        clients = _clients(policies=[policy])

        results = build_applicable_policies(clients, NAMESPACE, _target_pod())

        assert results[0].applies_to_target is True

    def test_match_labels_selector_matches(self):
        policy = _policy(pod_selector=V1LabelSelector(match_labels={"app": "target"}))
        clients = _clients(policies=[policy])

        results = build_applicable_policies(clients, NAMESPACE, _target_pod(labels={"app": "target"}))

        assert results[0].applies_to_target is True

    def test_match_labels_selector_does_not_match(self):
        policy = _policy(pod_selector=V1LabelSelector(match_labels={"app": "other"}))
        clients = _clients(policies=[policy])

        results = build_applicable_policies(clients, NAMESPACE, _target_pod(labels={"app": "target"}))

        assert results[0].applies_to_target is False

    def test_match_expressions_in_operator(self):
        selector = V1LabelSelector(
            match_expressions=[V1LabelSelectorRequirement(key="app", operator="In", values=["target", "other"])]
        )
        policy = _policy(pod_selector=selector)
        clients = _clients(policies=[policy])

        results = build_applicable_policies(clients, NAMESPACE, _target_pod(labels={"app": "target"}))

        assert results[0].applies_to_target is True

    def test_multiple_policies_evaluated_independently(self):
        matching = _policy(name="matching", pod_selector=V1LabelSelector(match_labels={"app": "target"}))
        not_matching = _policy(name="not-matching", pod_selector=V1LabelSelector(match_labels={"app": "other"}))
        clients = _clients(policies=[matching, not_matching])

        results = build_applicable_policies(clients, NAMESPACE, _target_pod(labels={"app": "target"}))

        by_name = {r.policy_name: r.applies_to_target for r in results}
        assert by_name == {"matching": True, "not-matching": False}


class TestPolicySummary:
    def test_default_policy_types_is_ingress_only(self):
        policy = _policy(policy_types=None)
        clients = _clients(policies=[policy])

        results = build_applicable_policies(clients, NAMESPACE, _target_pod())

        assert results[0].policy_types == ["Ingress"]

    def test_no_ingress_rules_means_deny_all(self):
        policy = _policy(policy_types=["Ingress"], ingress=[])
        clients = _clients(policies=[policy])

        results = build_applicable_policies(clients, NAMESPACE, _target_pod())

        assert "denies all ingress" in results[0].summary

    def test_ingress_rules_present_means_permits_matching_traffic(self):
        policy = _policy(policy_types=["Ingress"], ingress=[V1NetworkPolicyIngressRule()])
        clients = _clients(policies=[policy])

        results = build_applicable_policies(clients, NAMESPACE, _target_pod())

        assert "1 rule(s) would permit" in results[0].summary

    def test_egress_only_policy_does_not_mention_ingress(self):
        policy = _policy(policy_types=["Egress"], egress=[])
        clients = _clients(policies=[policy])

        results = build_applicable_policies(clients, NAMESPACE, _target_pod())

        assert "ingress" not in results[0].summary
        assert "denies all egress" in results[0].summary

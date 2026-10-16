from kubernetes.client import (
    V1LabelSelector,
    V1NetworkPolicy,
    V1NetworkPolicyIngressRule,
    V1NetworkPolicyPeer,
    V1NetworkPolicySpec,
    V1ObjectMeta,
)

from kubesentinel.config import Settings
from kubesentinel.security.inventory import SecurityInventory
from kubesentinel.security.rules.network_policy import (
    NamespaceWithoutNetworkPolicyRule,
    UnrestrictedIngressPolicyRule,
)

NAMESPACE = "kubesentinel-lab"


def _settings(**overrides) -> Settings:
    defaults = dict(optimization_excluded_namespaces="kube-system,kube-public,kube-node-lease,local-path-storage")
    defaults.update(overrides)
    return Settings(**defaults)


def _policy(name="policy", ingress=None, policy_types=None) -> V1NetworkPolicy:
    return V1NetworkPolicy(
        metadata=V1ObjectMeta(name=name, namespace=NAMESPACE),
        spec=V1NetworkPolicySpec(pod_selector=V1LabelSelector(), policy_types=policy_types, ingress=ingress),
    )


class TestNamespaceWithoutNetworkPolicyRule:
    def test_fires_for_namespace_with_pods_and_no_policies(self):
        inventory = SecurityInventory(namespaces_with_pods={NAMESPACE}, network_policies_by_namespace={NAMESPACE: []})

        findings = NamespaceWithoutNetworkPolicyRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert findings[0].resource.namespace == NAMESPACE

    def test_does_not_fire_when_a_policy_exists(self):
        inventory = SecurityInventory(
            namespaces_with_pods={NAMESPACE}, network_policies_by_namespace={NAMESPACE: [_policy()]}
        )

        assert NamespaceWithoutNetworkPolicyRule(_settings()).evaluate(inventory) == []

    def test_excludes_system_namespaces(self):
        inventory = SecurityInventory(namespaces_with_pods={"kube-system"}, network_policies_by_namespace={"kube-system": []})

        assert NamespaceWithoutNetworkPolicyRule(_settings()).evaluate(inventory) == []


class TestUnrestrictedIngressPolicyRule:
    def test_fires_for_unrestricted_ingress_rule(self):
        policy = _policy(ingress=[V1NetworkPolicyIngressRule()], policy_types=["Ingress"])
        inventory = SecurityInventory(network_policies_by_namespace={NAMESPACE: [policy]})

        findings = UnrestrictedIngressPolicyRule().evaluate(inventory)

        assert len(findings) == 1
        assert findings[0].resource.name == "policy"

    def test_does_not_fire_for_scoped_ingress_rule(self):
        policy = _policy(
            ingress=[V1NetworkPolicyIngressRule(_from=[V1NetworkPolicyPeer(pod_selector=V1LabelSelector())])],
            policy_types=["Ingress"],
        )
        inventory = SecurityInventory(network_policies_by_namespace={NAMESPACE: [policy]})

        assert UnrestrictedIngressPolicyRule().evaluate(inventory) == []

    def test_does_not_fire_for_deny_all_policy(self):
        policy = _policy(ingress=[], policy_types=["Ingress"])
        inventory = SecurityInventory(network_policies_by_namespace={NAMESPACE: [policy]})

        assert UnrestrictedIngressPolicyRule().evaluate(inventory) == []

    def test_does_not_fire_for_egress_only_policy(self):
        policy = _policy(ingress=[V1NetworkPolicyIngressRule()], policy_types=["Egress"])
        inventory = SecurityInventory(network_policies_by_namespace={NAMESPACE: [policy]})

        assert UnrestrictedIngressPolicyRule().evaluate(inventory) == []

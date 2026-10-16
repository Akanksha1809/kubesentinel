from kubernetes.client import (
    V1Deployment,
    V1DeploymentSpec,
    V1LabelSelector,
    V1LabelSelectorRequirement,
    V1ObjectMeta,
    V1PodDisruptionBudget,
    V1PodDisruptionBudgetSpec,
    V1PodTemplateSpec,
)

from kubesentinel.config import Settings
from kubesentinel.security.inventory import SecurityInventory
from kubesentinel.security.rules.availability import MissingPdbForMultiReplicaWorkloadRule, _pdb_selector_matches

NAMESPACE = "kubesentinel-lab"


def _settings(**overrides) -> Settings:
    defaults = dict(security_min_replicas_for_pdb_check=2)
    defaults.update(overrides)
    return Settings(**defaults)


def _deployment(name="demo", replicas=3, labels=None) -> V1Deployment:
    return V1Deployment(
        metadata=V1ObjectMeta(name=name, namespace=NAMESPACE),
        spec=V1DeploymentSpec(
            replicas=replicas,
            selector=V1LabelSelector(match_labels=labels or {"app": name}),
            template=V1PodTemplateSpec(metadata=V1ObjectMeta(labels=labels or {"app": name})),
        ),
    )


def _pdb(name="pdb", namespace=NAMESPACE, selector=None) -> V1PodDisruptionBudget:
    return V1PodDisruptionBudget(
        metadata=V1ObjectMeta(name=name, namespace=namespace),
        spec=V1PodDisruptionBudgetSpec(selector=selector if selector is not None else V1LabelSelector(match_labels={"app": "demo"})),
    )


class TestPdbSelectorMatches:
    def test_null_selector_matches_nothing(self):
        assert _pdb_selector_matches(None, {"app": "demo"}) is False

    def test_empty_present_selector_matches_everything(self):
        assert _pdb_selector_matches(V1LabelSelector(), {"app": "demo"}) is True

    def test_match_labels_matches(self):
        assert _pdb_selector_matches(V1LabelSelector(match_labels={"app": "demo"}), {"app": "demo"}) is True

    def test_match_labels_does_not_match(self):
        assert _pdb_selector_matches(V1LabelSelector(match_labels={"app": "other"}), {"app": "demo"}) is False

    def test_match_expressions_in_operator(self):
        selector = V1LabelSelector(match_expressions=[V1LabelSelectorRequirement(key="app", operator="In", values=["demo"])])
        assert _pdb_selector_matches(selector, {"app": "demo"}) is True


class TestMissingPdbForMultiReplicaWorkloadRule:
    def test_fires_for_uncovered_multi_replica_deployment(self):
        inventory = SecurityInventory(deployments=[_deployment(replicas=3)])

        findings = MissingPdbForMultiReplicaWorkloadRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert findings[0].resource.kind == "Deployment"

    def test_does_not_fire_when_covered_by_a_matching_pdb(self):
        inventory = SecurityInventory(deployments=[_deployment(replicas=3)], pod_disruption_budgets=[_pdb()])

        assert MissingPdbForMultiReplicaWorkloadRule(_settings()).evaluate(inventory) == []

    def test_does_not_fire_for_single_replica_workload(self):
        inventory = SecurityInventory(deployments=[_deployment(replicas=1)])

        assert MissingPdbForMultiReplicaWorkloadRule(_settings()).evaluate(inventory) == []

    def test_does_not_fire_when_pdb_is_in_a_different_namespace(self):
        inventory = SecurityInventory(
            deployments=[_deployment(replicas=3)],
            pod_disruption_budgets=[_pdb(namespace="other-namespace")],
        )

        assert len(MissingPdbForMultiReplicaWorkloadRule(_settings()).evaluate(inventory)) == 1

    def test_does_not_fire_when_pdb_selector_does_not_match_labels(self):
        inventory = SecurityInventory(
            deployments=[_deployment(replicas=3, labels={"app": "demo"})],
            pod_disruption_budgets=[_pdb(selector=V1LabelSelector(match_labels={"app": "other"}))],
        )

        assert len(MissingPdbForMultiReplicaWorkloadRule(_settings()).evaluate(inventory)) == 1

    def test_statefulset_also_covered(self):
        from kubernetes.client import V1StatefulSet, V1StatefulSetSpec

        statefulset = V1StatefulSet(
            metadata=V1ObjectMeta(name="demo-sts", namespace=NAMESPACE),
            spec=V1StatefulSetSpec(
                replicas=3,
                selector=V1LabelSelector(match_labels={"app": "demo-sts"}),
                template=V1PodTemplateSpec(metadata=V1ObjectMeta(labels={"app": "demo-sts"})),
                service_name="demo-sts",
            ),
        )
        inventory = SecurityInventory(stateful_sets=[statefulset])

        findings = MissingPdbForMultiReplicaWorkloadRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert findings[0].resource.kind == "StatefulSet"

from kubesentinel.config import Settings
from kubesentinel.security.inventory import SecurityInventory
from kubesentinel.security.rules.pod_security import NamespaceWithoutPodSecurityStandardRule

NAMESPACE = "kubesentinel-lab"


def _settings(**overrides) -> Settings:
    defaults = dict(optimization_excluded_namespaces="kube-system,kube-public,kube-node-lease,local-path-storage")
    defaults.update(overrides)
    return Settings(**defaults)


class TestNamespaceWithoutPodSecurityStandardRule:
    def test_fires_for_namespace_with_pods_and_no_psa_label(self):
        inventory = SecurityInventory(namespaces_with_pods={NAMESPACE}, namespace_labels={NAMESPACE: {}})

        findings = NamespaceWithoutPodSecurityStandardRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert findings[0].rule_id == "NAMESPACE_WITHOUT_POD_SECURITY_STANDARD"
        assert findings[0].resource.namespace == NAMESPACE
        assert findings[0].resource.kind == "Namespace"

    def test_does_not_fire_when_enforce_label_is_set(self):
        inventory = SecurityInventory(
            namespaces_with_pods={NAMESPACE},
            namespace_labels={NAMESPACE: {"pod-security.kubernetes.io/enforce": "baseline"}},
        )

        assert NamespaceWithoutPodSecurityStandardRule(_settings()).evaluate(inventory) == []

    def test_does_not_fire_when_only_audit_label_is_set(self):
        # audit/warn alone (no enforce) still means *some* PSA posture was
        # deliberately set for this namespace -- never treated the same as
        # nothing being set at all.
        inventory = SecurityInventory(
            namespaces_with_pods={NAMESPACE},
            namespace_labels={NAMESPACE: {"pod-security.kubernetes.io/audit": "restricted"}},
        )

        assert NamespaceWithoutPodSecurityStandardRule(_settings()).evaluate(inventory) == []

    def test_excludes_system_namespaces(self):
        inventory = SecurityInventory(namespaces_with_pods={"kube-system"}, namespace_labels={"kube-system": {}})

        assert NamespaceWithoutPodSecurityStandardRule(_settings()).evaluate(inventory) == []

    def test_does_not_fire_when_namespace_list_itself_failed(self):
        # namespace_labels has no entry at all for this namespace -- the
        # whole cluster-scoped LIST call failed this pass (see
        # SecurityInventory.namespace_labels's own docstring). Must never
        # be treated as "confirmed no labels," which would fabricate a
        # finding from a missing read, not real evidence.
        inventory = SecurityInventory(namespaces_with_pods={NAMESPACE}, namespace_labels={})

        assert NamespaceWithoutPodSecurityStandardRule(_settings()).evaluate(inventory) == []

    def test_namespace_with_genuinely_empty_labels_dict_still_fires(self):
        # Distinct from the case above: the namespace *is* present in
        # namespace_labels (the LIST call succeeded), it just genuinely
        # has no labels -- {} here means "confirmed empty," not "unknown."
        inventory = SecurityInventory(namespaces_with_pods={NAMESPACE}, namespace_labels={NAMESPACE: {}})

        findings = NamespaceWithoutPodSecurityStandardRule(_settings()).evaluate(inventory)

        assert len(findings) == 1

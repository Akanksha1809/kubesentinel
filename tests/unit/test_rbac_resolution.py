from kubernetes.client import (
    RbacV1Subject,
    V1ClusterRole,
    V1ClusterRoleBinding,
    V1ObjectMeta,
    V1PolicyRule,
    V1Role,
    V1RoleBinding,
    V1RoleRef,
)

from kubesentinel.security.inventory import SecurityInventory
from kubesentinel.security.rbac_resolution import (
    SubjectKey,
    bindings_granting_verb_on_rbac_objects,
    has_verb_on_resource,
    resolve_subject_bindings,
)

NAMESPACE = "kubesentinel-lab"


def _crb(name="crb", role_name="my-cluster-role", subjects=None) -> V1ClusterRoleBinding:
    return V1ClusterRoleBinding(
        metadata=V1ObjectMeta(name=name),
        role_ref=V1RoleRef(kind="ClusterRole", name=role_name, api_group="rbac.authorization.k8s.io"),
        subjects=subjects or [],
    )


def _rb(name="rb", namespace=NAMESPACE, role_kind="Role", role_name="my-role", subjects=None) -> V1RoleBinding:
    return V1RoleBinding(
        metadata=V1ObjectMeta(name=name, namespace=namespace),
        role_ref=V1RoleRef(kind=role_kind, name=role_name, api_group="rbac.authorization.k8s.io"),
        subjects=subjects or [],
    )


def _cluster_role(name="my-cluster-role", rules=None) -> V1ClusterRole:
    return V1ClusterRole(metadata=V1ObjectMeta(name=name), rules=rules or [])


def _role(name="my-role", namespace=NAMESPACE, rules=None) -> V1Role:
    return V1Role(metadata=V1ObjectMeta(name=name, namespace=namespace), rules=rules or [])


def _sa(name="my-sa", namespace=NAMESPACE) -> RbacV1Subject:
    return RbacV1Subject(kind="ServiceAccount", name=name, namespace=namespace)


class TestResolveSubjectBindings:
    def test_resolves_clusterrolebinding_to_its_clusterrole_rules(self):
        rule = V1PolicyRule(api_groups=["rbac.authorization.k8s.io"], resources=["roles"], verbs=["escalate"])
        inventory = SecurityInventory(
            cluster_role_bindings=[_crb(subjects=[_sa()])],
            cluster_roles=[_cluster_role(rules=[rule])],
        )

        result = resolve_subject_bindings(inventory)

        key = SubjectKey(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)
        assert key in result
        assert len(result[key]) == 1
        assert result[key][0].rules == [rule]
        assert result[key][0].binding_kind == "ClusterRoleBinding"

    def test_resolves_rolebinding_to_a_role_in_its_own_namespace(self):
        rule = V1PolicyRule(api_groups=[""], resources=["pods"], verbs=["get"])
        inventory = SecurityInventory(
            role_bindings=[_rb(role_kind="Role", role_name="my-role", subjects=[_sa()])],
            roles=[_role(rules=[rule])],
        )

        result = resolve_subject_bindings(inventory)

        key = SubjectKey(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)
        assert result[key][0].rules == [rule]
        assert result[key][0].role_kind == "Role"

    def test_resolves_rolebinding_to_a_clusterrole(self):
        # A documented, valid Kubernetes pattern: a RoleBinding scoping a
        # ClusterRole's rules to just the binding's own namespace.
        rule = V1PolicyRule(api_groups=[""], resources=["pods"], verbs=["get"])
        inventory = SecurityInventory(
            role_bindings=[_rb(role_kind="ClusterRole", role_name="my-cluster-role", subjects=[_sa()])],
            cluster_roles=[_cluster_role(rules=[rule])],
        )

        result = resolve_subject_bindings(inventory)

        key = SubjectKey(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)
        assert result[key][0].rules == [rule]
        assert result[key][0].role_kind == "ClusterRole"

    def test_unresolvable_clusterrolebinding_role_ref_is_skipped(self):
        inventory = SecurityInventory(
            cluster_role_bindings=[_crb(role_name="does-not-exist", subjects=[_sa()])],
            cluster_roles=[],
        )

        assert resolve_subject_bindings(inventory) == {}

    def test_unresolvable_rolebinding_role_ref_is_skipped(self):
        inventory = SecurityInventory(
            role_bindings=[_rb(role_kind="Role", role_name="does-not-exist", subjects=[_sa()])],
            roles=[],
        )

        assert resolve_subject_bindings(inventory) == {}

    def test_same_subject_across_multiple_bindings_accumulates_all_of_them(self):
        rule_a = V1PolicyRule(api_groups=[""], resources=["pods"], verbs=["get"])
        rule_b = V1PolicyRule(api_groups=[""], resources=["configmaps"], verbs=["list"])
        inventory = SecurityInventory(
            role_bindings=[
                _rb(name="rb-a", role_kind="Role", role_name="role-a", subjects=[_sa()]),
                _rb(name="rb-b", role_kind="Role", role_name="role-b", subjects=[_sa()]),
            ],
            roles=[_role(name="role-a", rules=[rule_a]), _role(name="role-b", rules=[rule_b])],
        )

        result = resolve_subject_bindings(inventory)

        key = SubjectKey(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)
        assert len(result[key]) == 2
        assert {b.binding_name for b in result[key]} == {"rb-a", "rb-b"}


class TestHasVerbOnResource:
    def test_exact_match(self):
        rule = V1PolicyRule(api_groups=["rbac.authorization.k8s.io"], resources=["roles"], verbs=["escalate"])
        assert has_verb_on_resource(rule, "escalate", "rbac.authorization.k8s.io", "roles") is True

    def test_wildcard_verb_does_not_satisfy_a_specific_verb_lookup(self):
        # Deliberate: that's WILDCARD_RBAC_RULE's own job, never
        # double-counted here under a different rule_id.
        rule = V1PolicyRule(api_groups=["rbac.authorization.k8s.io"], resources=["roles"], verbs=["*"])
        assert has_verb_on_resource(rule, "escalate", "rbac.authorization.k8s.io", "roles") is False

    def test_wildcard_resource_does_not_satisfy_a_specific_resource_lookup(self):
        rule = V1PolicyRule(api_groups=["rbac.authorization.k8s.io"], resources=["*"], verbs=["escalate"])
        assert has_verb_on_resource(rule, "escalate", "rbac.authorization.k8s.io", "roles") is False


class TestBindingsGrantingVerbOnRbacObjects:
    def test_matches_escalate_on_roles(self):
        from kubesentinel.security.rbac_resolution import ResolvedBinding

        binding = ResolvedBinding(
            binding_kind="ClusterRoleBinding",
            binding_name="crb",
            binding_namespace=None,
            role_kind="ClusterRole",
            role_name="my-cluster-role",
            rules=[V1PolicyRule(api_groups=["rbac.authorization.k8s.io"], resources=["roles"], verbs=["escalate"])],
        )

        matches = bindings_granting_verb_on_rbac_objects([binding], "escalate")
        assert matches == [(binding, "roles")]

    def test_no_match_for_unrelated_verb(self):
        from kubesentinel.security.rbac_resolution import ResolvedBinding

        binding = ResolvedBinding(
            binding_kind="ClusterRoleBinding",
            binding_name="crb",
            binding_namespace=None,
            role_kind="ClusterRole",
            role_name="my-cluster-role",
            rules=[V1PolicyRule(api_groups=["rbac.authorization.k8s.io"], resources=["roles"], verbs=["get"])],
        )

        assert bindings_granting_verb_on_rbac_objects([binding], "escalate") == []

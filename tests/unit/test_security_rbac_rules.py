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

from kubesentinel.config import Settings
from kubesentinel.security.inventory import SecurityInventory
from kubesentinel.security.rules.rbac import ClusterAdminBindingRule, EffectiveRbacEscalationRule, WildcardRbacRuleRule

NAMESPACE = "kubesentinel-lab"


def _settings(**overrides) -> Settings:
    defaults = dict(
        security_builtin_wildcard_role_names="cluster-admin,admin,edit,view",
        security_builtin_subject_group_prefixes="system:,kubeadm:",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _crb(name="crb", role_name="cluster-admin", subjects=None) -> V1ClusterRoleBinding:
    return V1ClusterRoleBinding(
        metadata=V1ObjectMeta(name=name),
        role_ref=V1RoleRef(kind="ClusterRole", name=role_name, api_group="rbac.authorization.k8s.io"),
        subjects=subjects or [],
    )


def _rb(name="rb", namespace=NAMESPACE, role_kind="ClusterRole", role_name="cluster-admin", subjects=None) -> V1RoleBinding:
    return V1RoleBinding(
        metadata=V1ObjectMeta(name=name, namespace=namespace),
        role_ref=V1RoleRef(kind=role_kind, name=role_name, api_group="rbac.authorization.k8s.io"),
        subjects=subjects or [],
    )


class TestClusterAdminBindingRule:
    def test_fires_for_serviceaccount_bound_to_cluster_admin(self):
        binding = _crb(subjects=[RbacV1Subject(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)])
        inventory = SecurityInventory(cluster_role_bindings=[binding])

        findings = ClusterAdminBindingRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert findings[0].severity.value == "CRITICAL"
        assert "my-sa" in findings[0].evidence[0]

    def test_does_not_fire_for_builtin_system_masters_binding(self):
        binding = _crb(subjects=[RbacV1Subject(kind="Group", name="system:masters")])
        inventory = SecurityInventory(cluster_role_bindings=[binding])

        assert ClusterAdminBindingRule(_settings()).evaluate(inventory) == []

    def test_does_not_fire_for_builtin_kubeadm_cluster_admins_binding(self):
        # Found live against this project's own kind cluster: a real
        # cluster has a *second* built-in cluster-admin Group binding
        # beyond system:masters -- kubeadm's own, used by admin.conf.
        binding = _crb(subjects=[RbacV1Subject(kind="Group", name="kubeadm:cluster-admins")])
        inventory = SecurityInventory(cluster_role_bindings=[binding])

        assert ClusterAdminBindingRule(_settings()).evaluate(inventory) == []

    def test_does_not_fire_for_non_cluster_admin_role(self):
        binding = _crb(role_name="view", subjects=[RbacV1Subject(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)])
        inventory = SecurityInventory(cluster_role_bindings=[binding])

        assert ClusterAdminBindingRule(_settings()).evaluate(inventory) == []

    def test_mixed_subjects_only_flags_the_non_builtin_one(self):
        binding = _crb(
            subjects=[
                RbacV1Subject(kind="Group", name="system:masters"),
                RbacV1Subject(kind="User", name="alice"),
            ]
        )
        inventory = SecurityInventory(cluster_role_bindings=[binding])

        findings = ClusterAdminBindingRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert "alice" in findings[0].evidence[0]

    def test_namespaced_rolebinding_to_cluster_admin_fires_high(self):
        binding = _rb(subjects=[RbacV1Subject(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)])
        inventory = SecurityInventory(role_bindings=[binding])

        findings = ClusterAdminBindingRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert findings[0].severity.value == "HIGH"
        assert findings[0].resource.namespace == NAMESPACE

    def test_rolebinding_to_a_plain_role_is_not_cluster_admin(self):
        binding = _rb(role_kind="Role", role_name="my-role", subjects=[RbacV1Subject(kind="ServiceAccount", name="my-sa")])
        inventory = SecurityInventory(role_bindings=[binding])

        assert ClusterAdminBindingRule(_settings()).evaluate(inventory) == []


def _cluster_role(name="my-role", rules=None) -> V1ClusterRole:
    return V1ClusterRole(metadata=V1ObjectMeta(name=name), rules=rules or [])


def _role(name="my-role", namespace=NAMESPACE, rules=None) -> V1Role:
    return V1Role(metadata=V1ObjectMeta(name=name, namespace=namespace), rules=rules or [])


class TestWildcardRbacRuleRule:
    def test_fires_for_wildcard_verb_and_resource(self):
        role = _cluster_role(rules=[V1PolicyRule(api_groups=[""], resources=["*"], verbs=["*"])])
        inventory = SecurityInventory(cluster_roles=[role])

        findings = WildcardRbacRuleRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert findings[0].severity.value == "HIGH"

    def test_fires_for_wildcard_verb_and_apigroup(self):
        role = _cluster_role(rules=[V1PolicyRule(api_groups=["*"], resources=["pods"], verbs=["*"])])
        inventory = SecurityInventory(cluster_roles=[role])

        assert len(WildcardRbacRuleRule(_settings()).evaluate(inventory)) == 1

    def test_does_not_fire_for_scoped_rule(self):
        role = _cluster_role(rules=[V1PolicyRule(api_groups=[""], resources=["pods"], verbs=["get", "list"])])
        inventory = SecurityInventory(cluster_roles=[role])

        assert WildcardRbacRuleRule(_settings()).evaluate(inventory) == []

    def test_wildcard_verb_alone_without_wildcard_resource_does_not_fire(self):
        role = _cluster_role(rules=[V1PolicyRule(api_groups=[""], resources=["pods"], verbs=["*"])])
        inventory = SecurityInventory(cluster_roles=[role])

        assert WildcardRbacRuleRule(_settings()).evaluate(inventory) == []

    def test_excludes_builtin_named_roles(self):
        role = _cluster_role(name="cluster-admin", rules=[V1PolicyRule(api_groups=["*"], resources=["*"], verbs=["*"])])
        inventory = SecurityInventory(cluster_roles=[role])

        assert WildcardRbacRuleRule(_settings()).evaluate(inventory) == []

    def test_excludes_system_prefixed_roles(self):
        role = _cluster_role(name="system:controller:foo", rules=[V1PolicyRule(api_groups=["*"], resources=["*"], verbs=["*"])])
        inventory = SecurityInventory(cluster_roles=[role])

        assert WildcardRbacRuleRule(_settings()).evaluate(inventory) == []

    def test_fires_for_namespaced_role_too(self):
        role = _role(rules=[V1PolicyRule(api_groups=["*"], resources=["*"], verbs=["*"])])
        inventory = SecurityInventory(roles=[role])

        findings = WildcardRbacRuleRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert findings[0].resource.namespace == NAMESPACE
        assert findings[0].resource.kind == "Role"


def _escalate_rule(resource: str = "roles") -> V1PolicyRule:
    return V1PolicyRule(api_groups=["rbac.authorization.k8s.io"], resources=[resource], verbs=["escalate"])


def _bind_rule(resource: str = "roles") -> V1PolicyRule:
    return V1PolicyRule(api_groups=["rbac.authorization.k8s.io"], resources=[resource], verbs=["bind"])


class TestEffectiveRbacEscalationRule:
    def test_fires_for_a_single_clusterrolebinding_granting_escalate(self):
        subject = RbacV1Subject(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)
        inventory = SecurityInventory(
            cluster_role_bindings=[_crb(role_name="my-role", subjects=[subject])],
            cluster_roles=[_cluster_role(rules=[_escalate_rule()])],
        )

        findings = EffectiveRbacEscalationRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert findings[0].severity.value == "CRITICAL"
        assert findings[0].resource.kind == "ServiceAccount"
        assert findings[0].resource.name == "my-sa"
        assert findings[0].resource.namespace == NAMESPACE

    def test_fires_for_a_single_rolebinding_granting_bind(self):
        subject = RbacV1Subject(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)
        inventory = SecurityInventory(
            role_bindings=[_rb(role_kind="Role", role_name="my-role", subjects=[subject])],
            roles=[_role(rules=[_bind_rule()])],
        )

        findings = EffectiveRbacEscalationRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert "bind" in findings[0].explanation

    def test_assembles_evidence_from_multiple_contributing_bindings(self):
        # The gap this rule exists to close: neither binding alone is the
        # full picture, but the subject is named by both.
        subject = RbacV1Subject(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)
        inventory = SecurityInventory(
            role_bindings=[
                _rb(name="rb-a", role_kind="Role", role_name="role-a", subjects=[subject]),
                _rb(name="rb-b", role_kind="Role", role_name="role-b", subjects=[subject]),
            ],
            roles=[
                _role(name="role-a", rules=[_escalate_rule(resource="roles")]),
                _role(name="role-b", rules=[_escalate_rule(resource="clusterroles")]),
            ],
        )

        findings = EffectiveRbacEscalationRule(_settings()).evaluate(inventory)

        assert len(findings) == 1
        assert len(findings[0].evidence) == 2
        assert "rb-a" in findings[0].evidence[0] or "rb-a" in findings[0].evidence[1]
        assert "rb-b" in findings[0].evidence[0] or "rb-b" in findings[0].evidence[1]
        assert "2 separate bindings" in findings[0].explanation

    def test_does_not_fire_without_escalate_or_bind(self):
        subject = RbacV1Subject(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)
        inventory = SecurityInventory(
            cluster_role_bindings=[_crb(role_name="my-role", subjects=[subject])],
            cluster_roles=[
                _cluster_role(
                    rules=[V1PolicyRule(api_groups=["rbac.authorization.k8s.io"], resources=["roles"], verbs=["get"])]
                )
            ],
        )

        assert EffectiveRbacEscalationRule(_settings()).evaluate(inventory) == []

    def test_wildcard_verb_alone_does_not_satisfy_this_rule(self):
        # Correctly left to WILDCARD_RBAC_RULE's own job, not
        # double-counted under this rule_id too.
        subject = RbacV1Subject(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)
        inventory = SecurityInventory(
            cluster_role_bindings=[_crb(role_name="my-role", subjects=[subject])],
            cluster_roles=[
                _cluster_role(rules=[V1PolicyRule(api_groups=["*"], resources=["*"], verbs=["*"])])
            ],
        )

        assert EffectiveRbacEscalationRule(_settings()).evaluate(inventory) == []

    def test_excludes_builtin_named_roles(self):
        subject = RbacV1Subject(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)
        inventory = SecurityInventory(
            cluster_role_bindings=[_crb(role_name="cluster-admin", subjects=[subject])],
            cluster_roles=[_cluster_role(name="cluster-admin", rules=[_escalate_rule()])],
        )

        assert EffectiveRbacEscalationRule(_settings()).evaluate(inventory) == []

    def test_excludes_system_prefixed_roles(self):
        subject = RbacV1Subject(kind="ServiceAccount", name="my-sa", namespace=NAMESPACE)
        inventory = SecurityInventory(
            cluster_role_bindings=[_crb(role_name="system:controller:foo", subjects=[subject])],
            cluster_roles=[_cluster_role(name="system:controller:foo", rules=[_escalate_rule()])],
        )

        assert EffectiveRbacEscalationRule(_settings()).evaluate(inventory) == []

    def test_group_subject_has_no_namespace_on_the_finding(self):
        subject = RbacV1Subject(kind="Group", name="my-group")
        inventory = SecurityInventory(
            cluster_role_bindings=[_crb(role_name="my-role", subjects=[subject])],
            cluster_roles=[_cluster_role(rules=[_escalate_rule()])],
        )

        findings = EffectiveRbacEscalationRule(_settings()).evaluate(inventory)

        assert findings[0].resource.kind == "Group"
        assert findings[0].resource.namespace is None

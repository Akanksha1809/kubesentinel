from kubesentinel.config import Settings
from kubesentinel.models.security import SecurityFinding, SecurityResourceRef, SecuritySeverity
from kubesentinel.security.engine import SecurityAuditEngine, default_rules
from kubesentinel.security.inventory import SecurityInventory
from kubesentinel.security.rules.base import SecurityRule


class _FakeRule(SecurityRule):
    rule_id = "FAKE_RULE"

    def __init__(self, findings=None):
        self._findings = findings or []

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        return self._findings


def _finding(rule_id="FAKE_RULE") -> SecurityFinding:
    return SecurityFinding(
        finding_id="f-1",
        rule_id=rule_id,
        severity=SecuritySeverity.HIGH,
        resource=SecurityResourceRef(namespace="kubesentinel-lab", kind="Pod", name="demo"),
        explanation="test",
        remediation="test",
    )


class TestSecurityAuditEngine:
    def test_collects_findings_from_every_rule(self):
        engine = SecurityAuditEngine(Settings(), rules=[_FakeRule([_finding()]), _FakeRule([_finding(), _finding()])])

        result = engine.audit(SecurityInventory(), namespaces=["kubesentinel-lab"])

        assert len(result.findings) == 3

    def test_rules_evaluated_lists_every_rule_id(self):
        engine = SecurityAuditEngine(Settings(), rules=[_FakeRule(), _FakeRule()])

        result = engine.audit(SecurityInventory(), namespaces=[])

        assert result.rules_evaluated == ["FAKE_RULE", "FAKE_RULE"]

    def test_empty_rule_list_produces_no_findings(self):
        engine = SecurityAuditEngine(Settings(), rules=[])

        result = engine.audit(SecurityInventory(), namespaces=[])

        assert result.findings == []

    def test_inventory_warnings_propagate_to_result(self):
        engine = SecurityAuditEngine(Settings(), rules=[])
        inventory = SecurityInventory(warnings=["could not list ClusterRoleBindings: Forbidden (status=403)"])

        result = engine.audit(inventory, namespaces=[])

        assert result.warnings == ["could not list ClusterRoleBindings: Forbidden (status=403)"]

    def test_determinism(self):
        engine = SecurityAuditEngine(Settings(), rules=[_FakeRule([_finding()])])
        inventory = SecurityInventory()

        r1 = engine.audit(inventory, namespaces=["kubesentinel-lab"])
        r2 = engine.audit(inventory, namespaces=["kubesentinel-lab"])

        assert len(r1.findings) == len(r2.findings)
        assert r1.rules_evaluated == r2.rules_evaluated

    def test_namespaces_carried_through(self):
        engine = SecurityAuditEngine(Settings(), rules=[])

        result = engine.audit(SecurityInventory(), namespaces=["ns-a", "ns-b"])

        assert result.namespaces == ["ns-a", "ns-b"]


class TestDefaultRules:
    def test_default_rules_includes_every_documented_rule_id(self):
        rules = default_rules(Settings())
        rule_ids = {r.rule_id for r in rules}

        assert rule_ids == {
            "PRIVILEGED_CONTAINER",
            "ALLOW_PRIVILEGE_ESCALATION",
            "RUN_AS_ROOT",
            "MISSING_READ_ONLY_ROOT_FS",
            "HOST_NAMESPACE_SHARING",
            "HOST_PATH_VOLUME",
            "MUTABLE_IMAGE_TAG",
            "SECRET_AS_ENV_VAR",
            "CLUSTER_ADMIN_BINDING",
            "WILDCARD_RBAC_RULE",
            "RBAC_SELF_ESCALATION_CAPABLE",
            "NAMESPACE_WITHOUT_NETWORK_POLICY",
            "UNRESTRICTED_INGRESS_POLICY",
            "MISSING_PDB_FOR_MULTI_REPLICA_WORKLOAD",
            "NAMESPACE_WITHOUT_POD_SECURITY_STANDARD",
        }

"""Unit tests for the deterministic pieces of EvaluationHarness --
`_scenario_label` and the whole-battery false-positive count
(docs/CHAOS_RESILIENCE_LAB.md §5). Everything else the harness does
(applying manifests via kubectl, driving a real ObserverService/
OptimizationService/etc. against a real cluster) is orchestration glue
over real services, not logic worth re-mocking at the unit tier -- it was
instead verified with a real, bounded `kubesentinel evaluate` run against
the live cluster (one scenario per capability; see docs/TESTING.md and
TODO.md's Milestone 26 entry for the live evidence), deliberately not
wired into the automated `--all` suite: a battery run deletes-then-
reapplies the scenarios it touches in the shared lab namespace
(docs/CHAOS_RESILIENCE_LAB.md §6), which is disruptive, developer-invoked
behavior this project does not want routine CI silently repeating.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

from kubesentinel.evaluation.harness import EvaluationHarness, _scenario_label
from kubesentinel.models.evaluation import EvaluationCapability
from kubesentinel.models.incident import Incident, IncidentSeverity, IncidentStatus, ResourceRef
from kubesentinel.models.network import DnsHealthResult, NetworkFinding, NetworkFindingClassification
from kubesentinel.models.optimization import (
    EvidenceQuality,
    RecommendationCategory,
    ResourceRecommendation,
    UsageSummary,
    WorkloadKind,
)
from kubesentinel.models.scheduling import SchedulingFinding
from kubesentinel.models.security import SecurityFinding, SecurityResourceRef, SecuritySeverity

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _harness_with_mock_repos() -> EvaluationHarness:
    harness = EvaluationHarness.__new__(EvaluationHarness)
    harness._incident_repo = MagicMock()
    harness._optimization_repo = MagicMock()
    harness._scheduling_repo = MagicMock()
    harness._network_repo = MagicMock()
    harness._security_repo = MagicMock()
    return harness


def _incident(name: str, status: IncidentStatus = IncidentStatus.DIAGNOSED) -> Incident:
    return Incident(
        incident_id=str(uuid4()),
        resource=ResourceRef(namespace="kubesentinel-lab", kind="Pod", name=name, uid=str(uuid4())),
        category="CRASH_LOOP_BACKOFF",
        severity=IncidentSeverity.HIGH,
        status=status,
        first_observed_at=NOW,
        last_observed_at=NOW,
    )


def _recommendation(workload_name: str) -> ResourceRecommendation:
    return ResourceRecommendation(
        recommendation_id=str(uuid4()),
        namespace="kubesentinel-lab",
        workload_kind=WorkloadKind.DEPLOYMENT,
        workload_name=workload_name,
        container_name="app",
        category=RecommendationCategory.DATA_QUALITY,
        usage=UsageSummary(evidence_quality=EvidenceQuality.INSUFFICIENT, observed_at=NOW),
        reasoning="test",
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )


def _scheduling_finding(workload_name: str) -> SchedulingFinding:
    return SchedulingFinding(
        finding_id=str(uuid4()),
        namespace="kubesentinel-lab",
        workload_name=workload_name,
        analyzed_at=NOW,
        reasoning="test",
    )


def _network_finding(workload_name: str) -> NetworkFinding:
    return NetworkFinding(
        finding_id=str(uuid4()),
        namespace="kubesentinel-lab",
        workload_name=workload_name,
        analyzed_at=NOW,
        dns_health=DnsHealthResult(),
        classification=NetworkFindingClassification.HEALTHY,
        reasoning="test",
    )


def _security_finding(workload_name: str | None) -> SecurityFinding:
    return SecurityFinding(
        finding_id=str(uuid4()),
        rule_id="PRIVILEGED_CONTAINER",
        severity=SecuritySeverity.HIGH,
        resource=SecurityResourceRef(namespace="kubesentinel-lab", kind="Pod", name="demo"),
        workload_name=workload_name,
        explanation="test",
        remediation="test",
    )


def test_scenario_label_strips_the_scenario_prefix():
    assert _scenario_label("scenario-oom") == "oom"
    assert _scenario_label("scenario-failed-mount") == "failed-mount"


class TestFalsePositiveCount:
    def test_known_resources_are_never_false_positives(self):
        harness = _harness_with_mock_repos()
        harness._incident_repo.list.return_value = [_incident("oom-demo-abc123")]
        harness._optimization_repo.list_recommendations.return_value = [_recommendation("missing-requests-demo")]
        harness._scheduling_repo.list_findings.return_value = []
        harness._network_repo.list_findings.return_value = []
        harness._security_repo.list_findings.return_value = []

        unattributed, total = harness._count_false_positives()
        assert total == 2
        assert unattributed == 0

    def test_a_resource_matching_no_registered_scenario_is_a_false_positive(self):
        harness = _harness_with_mock_repos()
        harness._incident_repo.list.return_value = [_incident("coredns-589f44dc88-gllxv")]
        harness._optimization_repo.list_recommendations.return_value = []
        harness._scheduling_repo.list_findings.return_value = []
        harness._network_repo.list_findings.return_value = []
        harness._security_repo.list_findings.return_value = []

        unattributed, total = harness._count_false_positives()
        assert total == 1
        assert unattributed == 1

    def test_resolved_incidents_are_excluded_entirely(self):
        harness = _harness_with_mock_repos()
        harness._incident_repo.list.return_value = [_incident("coredns-589f44dc88-gllxv", status=IncidentStatus.RESOLVED)]
        harness._optimization_repo.list_recommendations.return_value = []
        harness._scheduling_repo.list_findings.return_value = []
        harness._network_repo.list_findings.return_value = []
        harness._security_repo.list_findings.return_value = []

        unattributed, total = harness._count_false_positives()
        assert total == 0
        assert unattributed == 0

    def test_scheduling_and_network_findings_can_match_on_either_name_field(self):
        harness = _harness_with_mock_repos()
        harness._incident_repo.list.return_value = []
        harness._optimization_repo.list_recommendations.return_value = []
        harness._scheduling_repo.list_findings.return_value = [_scheduling_finding("pending-resources-demo")]
        harness._network_repo.list_findings.return_value = [_network_finding("port-mismatch-demo")]
        harness._security_repo.list_findings.return_value = [_security_finding("security-findings-demo")]

        unattributed, total = harness._count_false_positives()
        assert total == 3
        assert unattributed == 0

    def test_a_security_finding_with_no_workload_name_is_a_false_positive(self):
        # RBAC/NetworkPolicy/PDB findings genuinely have no owning workload
        # (models/security.py's own docstring) -- this harness has no way
        # to attribute one to a scenario, so it is honestly counted as
        # unattributed rather than silently excluded.
        harness = _harness_with_mock_repos()
        harness._incident_repo.list.return_value = []
        harness._optimization_repo.list_recommendations.return_value = []
        harness._scheduling_repo.list_findings.return_value = []
        harness._network_repo.list_findings.return_value = []
        harness._security_repo.list_findings.return_value = [_security_finding(None)]

        unattributed, total = harness._count_false_positives()
        assert total == 1
        assert unattributed == 1

    def test_empty_everywhere_is_zero_total_not_a_division_error(self):
        harness = _harness_with_mock_repos()
        harness._incident_repo.list.return_value = []
        harness._optimization_repo.list_recommendations.return_value = []
        harness._scheduling_repo.list_findings.return_value = []
        harness._network_repo.list_findings.return_value = []
        harness._security_repo.list_findings.return_value = []

        unattributed, total = harness._count_false_positives()
        assert (unattributed, total) == (0, 0)


def test_evaluation_capability_values_cover_every_dispatch_branch():
    # _check_scenario dispatches on every EvaluationCapability value --
    # pin the enum's own members so a future addition is forced to update
    # that dispatch rather than silently falling through to the SECURITY
    # branch (the final, unconditional `else` in _check_scenario).
    assert {c.value for c in EvaluationCapability} == {
        "TROUBLESHOOTING",
        "OPTIMIZATION",
        "SCHEDULING",
        "NETWORK",
        "SECURITY",
    }

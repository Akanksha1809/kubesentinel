from datetime import datetime, timezone

from kubesentinel.config import Settings
from kubesentinel.models.optimization import (
    EvidenceQuality,
    RecommendationCategory,
    ResourceQuantity,
    ResourceRecommendation,
    UsageSummary,
    WorkloadKind,
    WorkloadSnapshot,
)
from kubesentinel.optimization.safety import apply_safety_checks

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _workload(**overrides) -> WorkloadSnapshot:
    defaults = dict(
        namespace="kubesentinel-lab",
        workload_kind=WorkloadKind.DEPLOYMENT,
        workload_name="demo",
        workload_uid="uid-1",
        healthy=True,
        is_system_namespace=False,
    )
    defaults.update(overrides)
    return WorkloadSnapshot(**defaults)


def _recommendation() -> ResourceRecommendation:
    return ResourceRecommendation(
        recommendation_id="rec-1",
        namespace="kubesentinel-lab",
        workload_kind=WorkloadKind.DEPLOYMENT,
        workload_name="demo",
        container_name="app",
        category=RecommendationCategory.CPU,
        reasoning="test reasoning",
        usage=UsageSummary(evidence_quality=EvidenceQuality.LIMITED_HISTORY, observed_at=NOW),
        proposed_requests=ResourceQuantity(cpu_millicores=100),
        proposed_limits=ResourceQuantity(cpu_millicores=200),
        created_at=NOW,
        expires_at=NOW,
    )


def test_healthy_workload_in_normal_namespace_passes_through_unflagged():
    workload = _workload(healthy=True, is_system_namespace=False, workload_kind=WorkloadKind.DEPLOYMENT)
    recs = apply_safety_checks(workload, [_recommendation()], Settings())

    assert recs[0].safety_flags == []
    assert recs[0].proposed_requests is not None
    assert recs[0].proposed_limits is not None


def test_unhealthy_workload_flags_and_suppresses_proposal():
    workload = _workload(healthy=False)
    recs = apply_safety_checks(workload, [_recommendation()], Settings())

    assert "UNHEALTHY_WORKLOAD" in recs[0].safety_flags
    assert recs[0].proposed_requests is None
    assert recs[0].proposed_limits is None


def test_system_namespace_flags_and_suppresses_proposal():
    workload = _workload(is_system_namespace=True)
    recs = apply_safety_checks(workload, [_recommendation()], Settings())

    assert "SYSTEM_NAMESPACE" in recs[0].safety_flags
    assert recs[0].proposed_requests is None


def test_job_and_daemonset_are_flagged_conservative():
    for kind, flag in [(WorkloadKind.JOB, "JOB_CONSERVATIVE"), (WorkloadKind.DAEMONSET, "DAEMONSET_CONSERVATIVE")]:
        workload = _workload(workload_kind=kind)
        recs = apply_safety_checks(workload, [_recommendation()], Settings())
        assert flag in recs[0].safety_flags
        assert recs[0].proposed_requests is None


def test_statefulset_and_deployment_are_not_flagged_conservative():
    for kind in (WorkloadKind.DEPLOYMENT, WorkloadKind.STATEFULSET):
        workload = _workload(workload_kind=kind)
        recs = apply_safety_checks(workload, [_recommendation()], Settings())
        assert not any(f.endswith("_CONSERVATIVE") for f in recs[0].safety_flags)


def test_multiple_flags_can_stack_on_one_recommendation():
    workload = _workload(healthy=False, is_system_namespace=True)
    recs = apply_safety_checks(workload, [_recommendation()], Settings())

    assert "UNHEALTHY_WORKLOAD" in recs[0].safety_flags
    assert "SYSTEM_NAMESPACE" in recs[0].safety_flags


def test_preexisting_safety_flags_are_preserved_not_overwritten():
    workload = _workload(healthy=False)
    recommendation = _recommendation()
    recommendation.safety_flags = ["OOM_HISTORY"]
    recs = apply_safety_checks(workload, [recommendation], Settings())

    assert "OOM_HISTORY" in recs[0].safety_flags
    assert "UNHEALTHY_WORKLOAD" in recs[0].safety_flags


def test_empty_recommendation_list_returns_empty_list():
    workload = _workload()
    assert apply_safety_checks(workload, [], Settings()) == []


def test_active_incident_flags_suppresses_proposal_and_sets_related_ids():
    workload = _workload()
    recs = apply_safety_checks(workload, [_recommendation()], Settings(), related_incident_ids=["incident-1"])

    assert "ACTIVE_INCIDENT" in recs[0].safety_flags
    assert recs[0].proposed_requests is None
    assert recs[0].proposed_limits is None
    assert recs[0].related_incident_ids == ["incident-1"]


def test_multiple_related_incident_ids_are_all_recorded():
    workload = _workload()
    recs = apply_safety_checks(
        workload, [_recommendation()], Settings(), related_incident_ids=["incident-1", "incident-2"]
    )

    assert recs[0].related_incident_ids == ["incident-1", "incident-2"]


def test_no_related_incident_ids_leaves_recommendation_unflagged_for_it():
    workload = _workload()
    recs = apply_safety_checks(workload, [_recommendation()], Settings(), related_incident_ids=[])

    assert "ACTIVE_INCIDENT" not in recs[0].safety_flags
    assert recs[0].related_incident_ids == []
    assert recs[0].proposed_requests is not None

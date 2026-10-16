"""Registry-integrity tests for the evaluation harness's scenario list
(docs/CHAOS_RESILIENCE_LAB.md §2) -- catches a typo'd lab_dir, a duplicate
name, or a SCHEDULING/NETWORK entry missing the pod label it needs, before
any live run would discover it the slow way.
"""

from __future__ import annotations

from pathlib import Path

from kubesentinel.evaluation.scenarios import SCENARIOS, get_scenario
from kubesentinel.models.evaluation import EvaluationCapability

_LAB_MANIFESTS_ROOT = Path(__file__).resolve().parents[2] / "lab" / "manifests"


def test_every_scenario_name_is_unique():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))


def test_every_lab_dir_exists_on_disk():
    for spec in SCENARIOS:
        assert (_LAB_MANIFESTS_ROOT / spec.lab_dir).is_dir(), f"{spec.name}: {spec.lab_dir} does not exist"


def test_scheduling_and_network_scenarios_declare_a_pod_label():
    for spec in SCENARIOS:
        if spec.capability in (EvaluationCapability.SCHEDULING, EvaluationCapability.NETWORK):
            assert spec.pod_label_app, f"{spec.name} is {spec.capability.value} but has no pod_label_app"


def test_get_scenario_finds_a_known_name():
    spec = get_scenario("oom")
    assert spec is not None
    assert spec.capability == EvaluationCapability.TROUBLESHOOTING
    assert spec.expected == "OOM_KILLED"


def test_get_scenario_returns_none_for_an_unknown_name():
    assert get_scenario("not-a-real-scenario") is None


def test_transient_recovery_is_the_only_scenario_that_supports_recovery():
    recovery_scenarios = [s.name for s in SCENARIOS if s.supports_recovery]
    assert recovery_scenarios == ["transient-recovery"]


def test_oversized_resources_has_a_long_timeout():
    spec = get_scenario("oversized-resources")
    assert spec is not None
    assert spec.timeout_seconds >= 300.0


def test_pending_resources_is_registered_for_both_troubleshooting_and_scheduling():
    troubleshooting = get_scenario("pending-resources")
    scheduling = get_scenario("pending-resources-scheduling-fit")
    assert troubleshooting is not None
    assert scheduling is not None
    assert troubleshooting.lab_dir == scheduling.lab_dir
    assert troubleshooting.capability == EvaluationCapability.TROUBLESHOOTING
    assert scheduling.capability == EvaluationCapability.SCHEDULING

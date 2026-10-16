"""The evaluation harness's scenario registry -- see
docs/CHAOS_RESILIENCE_LAB.md §2 for why this is a hardcoded Python list
transcribing each scenario manifest's own already-reviewed "Expected
KubeSentinel diagnosis" comment, not a new YAML schema or a comment parser.

Two scenarios appear twice (`pending-resources`, under both
TROUBLESHOOTING and SCHEDULING): the lab reuses the same manifest for both
engines' own live verification already (see docs/SCHEDULING_ANALYSIS.md),
and this harness does the same rather than inventing a second manifest the
roadmap's own scope explicitly says not to add.
"""

from __future__ import annotations

from dataclasses import dataclass

from kubesentinel.models.evaluation import EvaluationCapability

_LAB_NAMESPACE = "kubesentinel-lab"
#: oversized-resources needs real metrics-server history to accumulate --
#: at least 3 samples spanning at least 5 minutes (its own manifest's
#: comment; see optimization_min_samples_for_history/
#: optimization_min_window_seconds_for_history). Every other scenario's
#: default of 60s is generous for a push-based or single-pass pull-based
#: detection.
_DEFAULT_TIMEOUT_SECONDS = 60.0
_OVERSIZED_RESOURCES_TIMEOUT_SECONDS = 400.0


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    lab_dir: str
    capability: EvaluationCapability
    #: How this scenario's own object is found among everything else live
    #: in the shared lab namespace -- a Pod/Deployment/PVC/Service name
    #: prefix (Pod names carry a random ReplicaSet/Pod suffix; every other
    #: kind here uses its exact, fixed name).
    resource_name_prefix: str
    #: The ground-truth value to match against what was actually observed
    #: -- a ReasonCode for TROUBLESHOOTING, a RecommendationCategory for
    #: OPTIMIZATION, a NetworkFindingClassification for NETWORK, a rule_id
    #: for SECURITY, or "UNSCHEDULABLE"/"SCHEDULABLE" for SCHEDULING.
    expected: str
    namespace: str = _LAB_NAMESPACE
    #: Only set for SCHEDULING/NETWORK, whose services analyze one real,
    #: already-existing Pod by name -- resolved at run time via this label,
    #: the same `app=<label>` lookup test_mount_rule_live.py already
    #: established as this project's own pattern for "find today's
    #: randomly-suffixed Pod name."
    pod_label_app: str | None = None
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    #: Only True for transient-recovery -- see
    #: docs/CHAOS_RESILIENCE_LAB.md §4 for why no other scenario attempts
    #: a recovery-time measurement at all.
    supports_recovery: bool = False
    #: An extra flag substring that must also appear on the matched
    #: recommendation -- only used by the two OPTIMIZATION scenarios where
    #: the category alone (CPU/MEMORY) isn't scenario-specific enough.
    required_flag_substring: str | None = None


SCENARIOS: list[ScenarioSpec] = [
    ScenarioSpec(
        name="crashloop",
        lab_dir="scenario-crashloop",
        capability=EvaluationCapability.TROUBLESHOOTING,
        resource_name_prefix="crashloop-demo",
        expected="CRASH_LOOP_BACKOFF",
    ),
    ScenarioSpec(
        name="failed-mount",
        lab_dir="scenario-failed-mount",
        capability=EvaluationCapability.TROUBLESHOOTING,
        resource_name_prefix="failed-mount-demo",
        expected="FAILED_MOUNT",
    ),
    ScenarioSpec(
        name="failed-rollout",
        lab_dir="scenario-failed-rollout",
        capability=EvaluationCapability.TROUBLESHOOTING,
        resource_name_prefix="failed-rollout-demo",
        expected="ROLLOUT_FAILED",
    ),
    ScenarioSpec(
        name="hpa-capacity-pressure",
        lab_dir="scenario-hpa-capacity-pressure",
        capability=EvaluationCapability.OPTIMIZATION,
        resource_name_prefix="hpa-capacity-pressure-demo",
        expected="CAPACITY",
        required_flag_substring="HPA_AT_MAX",
    ),
    ScenarioSpec(
        name="imagepull",
        lab_dir="scenario-imagepull",
        capability=EvaluationCapability.TROUBLESHOOTING,
        resource_name_prefix="imagepull-demo",
        # Either is a correct detection -- the manifest's own comment names
        # both as acceptable depending on which message text the kubelet
        # happens to report.
        expected="IMAGE_PULL_BACKOFF",
    ),
    ScenarioSpec(
        name="invalid-node-selector",
        lab_dir="scenario-invalid-node-selector",
        capability=EvaluationCapability.TROUBLESHOOTING,
        resource_name_prefix="invalid-node-selector-demo",
        expected="PENDING_SCHEDULING_CONSTRAINT",
    ),
    ScenarioSpec(
        name="missing-requests",
        lab_dir="scenario-missing-requests",
        capability=EvaluationCapability.OPTIMIZATION,
        resource_name_prefix="missing-requests-demo",
        expected="DATA_QUALITY",
    ),
    ScenarioSpec(
        name="oom",
        lab_dir="scenario-oom",
        capability=EvaluationCapability.TROUBLESHOOTING,
        resource_name_prefix="oom-demo",
        expected="OOM_KILLED",
    ),
    ScenarioSpec(
        name="oversized-resources",
        lab_dir="scenario-oversized-resources",
        capability=EvaluationCapability.OPTIMIZATION,
        resource_name_prefix="oversized-resources-demo",
        expected="CPU",
        required_flag_substring="OVERSIZED",
        timeout_seconds=_OVERSIZED_RESOURCES_TIMEOUT_SECONDS,
    ),
    ScenarioSpec(
        name="pending-resources",
        lab_dir="scenario-pending-resources",
        capability=EvaluationCapability.TROUBLESHOOTING,
        resource_name_prefix="pending-resources-demo",
        expected="PENDING_INSUFFICIENT_RESOURCES",
    ),
    ScenarioSpec(
        name="pending-resources-scheduling-fit",
        lab_dir="scenario-pending-resources",
        capability=EvaluationCapability.SCHEDULING,
        resource_name_prefix="pending-resources-demo",
        pod_label_app="pending-resources-demo",
        expected="UNSCHEDULABLE",
    ),
    ScenarioSpec(
        name="pvc-binding",
        lab_dir="scenario-pvc-binding",
        capability=EvaluationCapability.TROUBLESHOOTING,
        resource_name_prefix="pvc-binding-demo",
        expected="PVC_BINDING_FAILURE",
    ),
    ScenarioSpec(
        name="readiness-probe-failure",
        lab_dir="scenario-readiness-probe-failure",
        capability=EvaluationCapability.TROUBLESHOOTING,
        resource_name_prefix="readiness-probe-failure-demo",
        expected="READINESS_PROBE_FAILING",
    ),
    ScenarioSpec(
        name="security-findings",
        lab_dir="scenario-security-findings",
        capability=EvaluationCapability.SECURITY,
        resource_name_prefix="security-findings-demo",
        expected="PRIVILEGED_CONTAINER",
    ),
    ScenarioSpec(
        name="service-port-mismatch",
        lab_dir="scenario-service-port-mismatch",
        capability=EvaluationCapability.NETWORK,
        resource_name_prefix="port-mismatch-demo",
        pod_label_app="port-mismatch-demo",
        expected="CONFIGURATION_ERROR",
    ),
    ScenarioSpec(
        name="service-selector-mismatch",
        lab_dir="scenario-service-selector-mismatch",
        capability=EvaluationCapability.TROUBLESHOOTING,
        resource_name_prefix="service-selector-mismatch-demo",
        expected="SERVICE_SELECTOR_MISMATCH",
    ),
    ScenarioSpec(
        name="transient-recovery",
        lab_dir="scenario-transient-recovery",
        capability=EvaluationCapability.TROUBLESHOOTING,
        resource_name_prefix="transient-recovery-demo",
        expected="CRASH_LOOP_BACKOFF",
        supports_recovery=True,
    ),
]

_BY_NAME = {s.name: s for s in SCENARIOS}


def get_scenario(name: str) -> ScenarioSpec | None:
    return _BY_NAME.get(name)

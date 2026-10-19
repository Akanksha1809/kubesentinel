"""Resource optimization models: workload resource snapshots and the
recommendations derived from them.

Kept separate from `models/incident.py`/`models/diagnosis.py` deliberately --
a `ResourceRecommendation` is a different aggregate with a different
lifecycle (new/reviewed/approved/rejected/expired, never "resolved" the way
an incident is), not a specialization of `Incident`. `related_incident_ids`
is the only link between the two, by ID, never a live object reference.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from kubesentinel.models.completeness import EvidenceCompletenessMixin


class RecommendationCategory(str, Enum):
    CPU = "CPU"
    MEMORY = "MEMORY"
    SCHEDULING = "SCHEDULING"
    # A workload this project cannot responsibly analyze yet -- missing
    # requests/limits entirely, or insufficient evidence -- is still reported
    # (never silently dropped), just under this category instead of CPU/MEMORY.
    DATA_QUALITY = "DATA_QUALITY"
    # HPA status/replica-capacity reasoning (Milestone 5 of the platform
    # spec) -- see docs/CAPACITY_PLANNING.md. Never carries proposed_requests/
    # proposed_limits, the same shape SCHEDULING already uses.
    CAPACITY = "CAPACITY"


class EvidenceQuality(str, Enum):
    """How much real data backs a recommendation -- distinct from
    `diagnosis.Confidence`, which is "how sure is this hypothesis." A
    recommendation can be built on `HISTORICAL`-quality evidence and still be
    wrong about the right number; this field only ever describes the data,
    never the conclusion.
    """

    INSUFFICIENT = "insufficient"  # no usable samples; `proposed` is always None
    SNAPSHOT_ONLY = "snapshot_only"  # one or a few metrics-server current samples
    LIMITED_HISTORY = "limited_history"  # bounded in-memory sample window, no Prometheus
    HISTORICAL = "historical"  # a real Prometheus range query covering the window


class RecommendationStatus(str, Enum):
    NEW = "NEW"
    REVIEWED = "REVIEWED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    # Still deliberately no APPLIED/FAILED here, even though an apply path
    # now exists (RESOURCE_OPTIMIZATION_DESIGN.md Section 14): this status
    # describes a human's own review judgment ("is this number sound"),
    # kept cleanly separate from whether applying it succeeded -- that's
    # answered by following ResourceRecommendation.applied_action_id to
    # the RemediationAction's own status instead, never by overloading
    # this enum with a second, unrelated kind of state.


class WorkloadKind(str, Enum):
    DEPLOYMENT = "Deployment"
    STATEFULSET = "StatefulSet"
    DAEMONSET = "DaemonSet"
    JOB = "Job"
    POD = "Pod"  # standalone Pod with no recognized controller owner


class ResourceQuantity(BaseModel):
    """Normalized resource amounts -- always already-parsed ints (millicores,
    bytes), never a raw Kubernetes quantity string. See optimization/
    quantities.py, the only place a string is parsed into this shape."""

    cpu_millicores: int | None = None
    memory_bytes: int | None = None


class UsageSummary(BaseModel):
    evidence_quality: EvidenceQuality
    sample_count: int = 0
    window_seconds: float | None = None
    observed_at: datetime
    cpu_typical_millicores: int | None = None
    cpu_peak_millicores: int | None = None
    memory_typical_bytes: int | None = None
    memory_peak_bytes: int | None = None
    # None means "unknown" (no throttling data available) -- never coerced to
    # False, which would read as "confirmed not throttled."
    throttled: bool | None = None

    @classmethod
    def insufficient(cls, observed_at: datetime | None = None) -> UsageSummary:
        """The honest placeholder for "no metrics have been attached yet" --
        used by `inventory.py` when building a `WorkloadSnapshot` purely from
        Kubernetes objects, before any metrics provider has been consulted
        (that's `analyzer.py`'s job, Milestone 4). Never a zero-filled
        `UsageSummary`, which would misread as "confirmed zero usage.\""""
        return cls(evidence_quality=EvidenceQuality.INSUFFICIENT, observed_at=observed_at or datetime.now(timezone.utc))


class TrendDirection(str, Enum):
    """A simple two-half-mean comparison over an already-sampled window --
    explicitly a description of what was observed, never a forecast. See
    docs/CAPACITY_PLANNING.md §2/§4."""

    INCREASING = "INCREASING"
    DECREASING = "DECREASING"
    STABLE = "STABLE"
    INSUFFICIENT = "INSUFFICIENT"  # not enough samples/window to say anything


class HpaMetricStatus(BaseModel):
    """One of an HPA's configured metrics, current value vs. target, read
    verbatim from the real object -- never computed or inferred here."""

    metric_name: str
    metric_type: str
    target_utilization_percent: int | None = None
    target_average_value: str | None = None
    current_utilization_percent: int | None = None
    current_average_value: str | None = None


class HpaSnapshot(BaseModel):
    """A real HorizontalPodAutoscaler's status, matched to a WorkloadSnapshot
    by scaleTargetRef kind+name (an HPA has no UID reference to its target,
    only a name -- see optimization/capacity.py::attach_hpa_snapshots)."""

    hpa_name: str
    namespace: str
    scale_target_kind: str
    scale_target_name: str
    min_replicas: int | None = None
    max_replicas: int
    current_replicas: int
    desired_replicas: int
    metrics: list[HpaMetricStatus] = Field(default_factory=list)
    # The HPA's own ScalingLimited condition, read verbatim -- never
    # inferred from current_replicas == max_replicas alone (that could
    # also mean "at max and no longer under pressure").
    scaling_limited: bool = False
    scaling_limited_reason: str | None = None


class ContainerResourceSnapshot(BaseModel):
    """One container's configured resources, within a WorkloadSnapshot."""

    container_name: str
    requests: ResourceQuantity = Field(default_factory=ResourceQuantity)
    limits: ResourceQuantity = Field(default_factory=ResourceQuantity)
    usage: UsageSummary = Field(default_factory=UsageSummary.insufficient)
    # Set at inventory time from this container's own Pods' `lastState`/
    # `state` termination reason. Read by analyzer.py to increase scrutiny
    # before ever proposing a *decreased* memory limit -- the spec's "a
    # historical OOM kill should increase scrutiny rather than automatically
    # trigger a larger limit" cuts both ways: it also must not be waved away
    # by a since-quiet usage window.
    recent_oom_detected: bool = False


class WorkloadSnapshot(BaseModel):
    """One workload (a Deployment/StatefulSet/DaemonSet/Job, or a standalone
    Pod), with its owner chain already resolved -- never a bare Pod list for
    a controller-owned workload. See optimization/ownership.py."""

    namespace: str
    workload_kind: WorkloadKind
    workload_name: str
    workload_uid: str
    containers: list[ContainerResourceSnapshot] = Field(default_factory=list)
    desired_replicas: int | None = None
    ready_replicas: int | None = None
    pod_uids: list[str] = Field(default_factory=list)
    # Parallel to pod_uids (same order, same length) -- needed to match
    # metrics-server's name-keyed usage samples back to this workload, since
    # the Metrics API has no concept of a Pod's UID at all, only its name.
    pod_names: list[str] = Field(default_factory=list)
    # True only when every Pod currently backing this workload is healthy
    # (Ready, no recent OOMKilled, restart count not actively climbing).
    # Unknown/empty workloads (no Pods observed at all) are also not healthy,
    # since there's nothing to safely optimize against -- see safety.py.
    healthy: bool = False
    is_system_namespace: bool = False
    # The scheduler's own message, set only when at least one of this
    # workload's Pods currently shows `PodScheduled=False, reason=
    # Unschedulable` -- reuses diagnostics/rules/scheduling.py's existing
    # message classification (by import, not a copy) to decide whether this
    # is a SCHEDULING-category recommendation. None means every observed Pod
    # was schedulable, not "unknown."
    unschedulable_reason: str | None = None
    # None means no HPA targets this workload -- never a placeholder/zeroed
    # HpaSnapshot, which would misread as "an HPA exists and reports zero."
    hpa: HpaSnapshot | None = None


class ResourceRecommendation(BaseModel):
    recommendation_id: str
    namespace: str
    workload_kind: WorkloadKind
    workload_name: str
    # The WorkloadSnapshot's own UID this recommendation was derived from --
    # None only for hand-built recommendations (e.g. test fixtures) that
    # never went through the real inventory; every recommendation a real
    # analyze() pass produces always sets this. Used by
    # OptimizationService.apply_recommendation() as the ActionTarget's own
    # resource_uid, the same "has this resource changed since proposal"
    # identity check every other action type already gets from
    # incident.resource.uid (see RESOURCE_OPTIMIZATION_DESIGN.md Section 14.2).
    workload_uid: str | None = None
    container_name: str
    category: RecommendationCategory

    current_requests: ResourceQuantity = Field(default_factory=ResourceQuantity)
    current_limits: ResourceQuantity = Field(default_factory=ResourceQuantity)
    usage: UsageSummary
    # None whenever evidence_quality is INSUFFICIENT, or the category is
    # SCHEDULING/DATA_QUALITY (those categories describe a condition, not a
    # proposed number) -- never a fabricated or zero-filled placeholder.
    proposed_requests: ResourceQuantity | None = None
    proposed_limits: ResourceQuantity | None = None

    reasoning: str
    potential_benefits: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    related_incident_ids: list[str] = Field(default_factory=list)
    # e.g. "ACTIVE_INCIDENT", "UNHEALTHY_WORKLOAD", "SYSTEM_NAMESPACE",
    # "JOB_EXCLUDED", "MISSING_REQUESTS", "MISSING_LIMITS",
    # "OOM_HISTORY" -- see optimization/safety.py for the full set.
    safety_flags: list[str] = Field(default_factory=list)

    status: RecommendationStatus = RecommendationStatus.NEW
    created_at: datetime
    expires_at: datetime

    # Set once OptimizationService.apply_recommendation() successfully
    # creates a standalone RemediationAction for this recommendation --
    # None means "never applied." Same ID-only link pattern as
    # related_incident_ids: never a live object reference, and this
    # recommendation's own `status` is never overloaded to also mean
    # "application succeeded/failed" (see RecommendationStatus's own
    # docstring above) -- follow this ID to the action's own status for
    # that instead.
    applied_action_id: str | None = None

    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_notes: str | None = None


class NodeOvercommitment(BaseModel):
    """One Node's requested/limits-vs-allocatable picture --
    `optimization/overcommitment.py`'s per-Node row. `requested_*_ratio`
    should never exceed 1.0 in a healthy cluster (the real scheduler
    refuses to bind a Pod past allocatable); a ratio above 1.0 here is
    itself a real anomaly worth surfacing, not just a formatting choice.
    `limits_*_ratio` is the actionable signal: Kubernetes never enforces
    this at admission, so it can run past 1.0 in perfectly normal
    operation -- whether that's a problem is a judgment call `
    overcommitted_cpu`/`overcommitted_memory` makes against a configurable
    threshold, not a hardcoded "any overage is bad.\""""

    node_name: str
    allocatable: ResourceQuantity
    requested: ResourceQuantity
    limits: ResourceQuantity
    # None (not 0.0) when allocatable for that resource is unknown --
    # never a ratio computed against a missing denominator.
    requested_cpu_ratio: float | None = None
    requested_memory_ratio: float | None = None
    limits_cpu_ratio: float | None = None
    limits_memory_ratio: float | None = None
    # How many containers on this Node specify no limit at all for that
    # resource -- `limits_*_ratio` only ever reflects containers that
    # *did* specify one, so this is the honest caveat that the real
    # worst case is unbounded, not just whatever the ratio says.
    unlimited_cpu_container_count: int = 0
    unlimited_memory_container_count: int = 0
    overcommitted_cpu: bool = False
    overcommitted_memory: bool = False
    # Real, currently-observed usage from the Kubernetes Metrics API
    # (`metrics.k8s.io/nodes`) -- the most actionable of the three
    # signals here, since requested/limits are both configuration, not
    # fact. `None` whenever no metrics-server is installed or reachable
    # (this project's own lab has none) -- never a zero-filled
    # substitute for "no usage data available."
    actual_cpu_millicores: int | None = None
    actual_memory_bytes: int | None = None
    actual_cpu_ratio: float | None = None
    actual_memory_ratio: float | None = None
    # True once actual usage exceeds `optimization_node_pressure_ratio_threshold`
    # of this Node's own allocatable -- unlike `overcommitted_*` above,
    # this is never a configuration anomaly, it is this Node genuinely
    # under real load right now.
    under_pressure_cpu: bool = False
    under_pressure_memory: bool = False


class ClusterOvercommitmentSummary(BaseModel):
    """Cluster-wide resource overcommitment, aggregated across every Node
    (RESOURCE_OPTIMIZATION_DESIGN.md's own named "cluster-level
    overcommitment analysis" gap). Deliberately its own top-level summary,
    never a `ResourceRecommendation` -- a per-container recommendation
    shape has no way to express "this is true of the whole cluster, not
    any one workload." See `optimization/overcommitment.py`."""

    computed_at: datetime
    nodes: list[NodeOvercommitment] = Field(default_factory=list)
    cluster_requested_cpu_ratio: float | None = None
    cluster_requested_memory_ratio: float | None = None
    cluster_limits_cpu_ratio: float | None = None
    cluster_limits_memory_ratio: float | None = None
    overcommitted_cpu: bool = False
    overcommitted_memory: bool = False
    # Cluster-wide real usage, same honest-absence rule as the per-Node
    # fields above: None whenever Node metrics weren't available this
    # pass, never a number that silently excludes unreachable Nodes.
    cluster_actual_cpu_ratio: float | None = None
    cluster_actual_memory_ratio: float | None = None
    under_pressure_cpu: bool = False
    under_pressure_memory: bool = False


class AnalysisRunSummary(EvidenceCompletenessMixin):
    """One `/analyze` pass's own metadata -- the thing `GET /optimization/summary`
    uses to answer "how fresh is this, and did metrics actually work" without
    ever re-scanning the cluster on a GET. Only the latest run is kept (see
    `persistence/optimization_repo.py`) -- this is a freshness marker, not a
    history log.

    `metrics_available`/`warning` are this model's own original fields,
    kept exactly as they were; `warnings`/`evidence_completeness`
    (inherited from `EvidenceCompletenessMixin`) are the newer, shared
    vocabulary set alongside them from the same information, never a
    replacement -- see `models/completeness.py`."""

    analyzed_at: datetime
    namespaces: list[str] = Field(default_factory=list)
    workload_count: int
    recommendation_count: int
    # False whenever the metrics provider could not be reached at all for
    # this run (metrics-server down/missing/permission-denied) -- distinct
    # from "reachable but returned no data for this workload," which is a
    # per-container EvidenceQuality, not a run-level failure. Never silently
    # reported as a successful analysis when this is False -- see the
    # spec's "do not report a successful analysis when the metrics provider
    # failed."
    metrics_available: bool
    warning: str | None = None
    # None whenever Node inventory wasn't available for this run (the same
    # degrade-don't-abort case `analyze()` already handles for the
    # capacity-headroom check) -- never a zero-filled summary, which would
    # misread as "confirmed not overcommitted."
    overcommitment: ClusterOvercommitmentSummary | None = None

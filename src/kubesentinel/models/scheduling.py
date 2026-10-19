"""Scheduling & placement advisor models: per-constraint evaluation results,
per-node fit, and the overall finding for one Pod (real or hypothetical).

`ResourceQuantity` is imported from `models.optimization` rather than
redefined -- it's already exactly "normalized, already-parsed resource
amounts," with nothing optimization-specific about its shape. See
docs/SCHEDULING_ANALYSIS.md Section 1 for why this and a few other things
(`optimization/quantities.py`'s parsing functions) are reused by import
across the two capability packages instead of duplicated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from kubesentinel.models.optimization import ResourceQuantity


class ConstraintType(str, Enum):
    """See docs/SCHEDULING_ANALYSIS.md Section 2 for exactly which of these
    are evaluated for real (end up in a finding's `evaluated_constraints`)
    vs. only detected and reported (always in `unevaluated_constraints`,
    never contribute a `ConstraintResult`)."""

    RESOURCE_FIT = "RESOURCE_FIT"
    TAINT_TOLERATION = "TAINT_TOLERATION"
    NODE_SELECTOR = "NODE_SELECTOR"
    REQUIRED_NODE_AFFINITY = "REQUIRED_NODE_AFFINITY"
    NAMESPACE_QUOTA = "NAMESPACE_QUOTA"
    # Closes docs/SCHEDULING_ANALYSIS.md's own named "Admission constraints
    # generally: Partial -- only ResourceQuota; no LimitRange" gap. Same
    # admission-time-only framing as NAMESPACE_QUOTA just above -- gated by
    # the same `check_quota` flag, meaningful only for a hypothetical spec.
    LIMIT_RANGE = "LIMIT_RANGE"
    # Added to close docs/SCHEDULING_ANALYSIS.md Section 2's own named
    # gap -- the same required-vs-preferred split REQUIRED_NODE_AFFINITY/
    # PREFERRED_NODE_AFFINITY already established, applied here too.
    # TOPOLOGY_SPREAD has no such split (whenUnsatisfiable is per-constraint,
    # not a separate preferred list), so every topologySpreadConstraint is
    # evaluated for real.
    REQUIRED_POD_AFFINITY = "REQUIRED_POD_AFFINITY"
    REQUIRED_POD_ANTI_AFFINITY = "REQUIRED_POD_ANTI_AFFINITY"
    TOPOLOGY_SPREAD = "TOPOLOGY_SPREAD"
    # Detected, never evaluated in v1 -- see the design doc. POD_AFFINITY/
    # POD_ANTI_AFFINITY now specifically mean "the preferred half, or a
    # required term this engine can't evaluate (namespaceSelector)" --
    # never the required terms that *can* be evaluated, which are
    # REQUIRED_POD_AFFINITY/REQUIRED_POD_ANTI_AFFINITY above instead.
    PREFERRED_NODE_AFFINITY = "PREFERRED_NODE_AFFINITY"
    POD_AFFINITY = "POD_AFFINITY"
    POD_ANTI_AFFINITY = "POD_ANTI_AFFINITY"


#: Constraints this engine can actually evaluate pass/fail for. Anything
#: else detected on a Pod spec is reported in `unevaluated_constraints`
#: instead of guessed at. Kept next to `ConstraintType` so the two can never
#: silently drift apart.
EVALUATED_CONSTRAINT_TYPES = frozenset(
    {
        ConstraintType.RESOURCE_FIT,
        ConstraintType.TAINT_TOLERATION,
        ConstraintType.NODE_SELECTOR,
        ConstraintType.REQUIRED_NODE_AFFINITY,
        ConstraintType.NAMESPACE_QUOTA,
        ConstraintType.LIMIT_RANGE,
        ConstraintType.REQUIRED_POD_AFFINITY,
        ConstraintType.REQUIRED_POD_ANTI_AFFINITY,
        ConstraintType.TOPOLOGY_SPREAD,
    }
)


class NodeTaint(BaseModel):
    key: str
    value: str | None = None
    effect: str  # NoSchedule | PreferNoSchedule | NoExecute


class NodeSnapshot(BaseModel):
    """One Node's scheduling-relevant state -- allocatable capacity, already-
    requested resources (the scheduler's own accounting unit: the sum of
    every *other* Pod's requests currently bound to this Node, not live
    usage), taints, and labels."""

    name: str
    allocatable: ResourceQuantity
    requested: ResourceQuantity
    unschedulable: bool = False  # Node.spec.unschedulable (cordoned)
    taints: list[NodeTaint] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    ready: bool = True
    # Sum of every bound container's own `limits` that actually specifies
    # one -- never a stand-in for "total possible consumption," since a
    # missing limit is a real, unbounded ceiling, not zero. See the
    # `unlimited_*_container_count` fields below for that case.
    limits: ResourceQuantity = Field(default_factory=ResourceQuantity)
    # How many containers on this Node specify no cpu/memory limit at all --
    # this Node's real worst-case consumption is unbounded for that
    # resource whenever this is > 0, regardless of what `limits` above
    # sums to. Used by `optimization/overcommitment.py` to avoid ever
    # reporting a false sense of safety from a limits-vs-allocatable ratio
    # alone.
    unlimited_cpu_container_count: int = 0
    unlimited_memory_container_count: int = 0


class PodLabelSummary(BaseModel):
    """The minimum a pod affinity/anti-affinity or topology-spread check
    needs about one already-scheduled Pod -- never the full Pod object,
    since nothing here reads containers/status. `uid` is used only to
    exclude a real candidate Pod from matching against itself when
    re-analyzing a Pod that already exists (a hypothetical spec has no
    `uid` at all, so nothing is excluded for it)."""

    uid: str
    namespace: str
    node_name: str
    labels: dict[str, str] = Field(default_factory=dict)


class ConstraintResult(BaseModel):
    constraint_type: ConstraintType
    satisfied: bool
    reasoning: str


class NodeFitResult(BaseModel):
    node_name: str
    schedulable: bool  # AND of every evaluated constraint's `satisfied` on this node
    constraint_results: list[ConstraintResult] = Field(default_factory=list)


class SchedulingFinding(BaseModel):
    finding_id: str
    namespace: str
    pod_name: str | None = None  # None for a hypothetical (not-yet-created) spec
    workload_kind: str | None = None
    workload_name: str | None = None
    analyzed_at: datetime
    #: The real PodScheduled condition message, when analyzing an actual
    #: Pending Pod -- None for a hypothetical spec, which has no condition yet.
    scheduler_message: str | None = None
    node_results: list[NodeFitResult] = Field(default_factory=list)
    evaluated_constraints: list[ConstraintType] = Field(default_factory=list)
    unevaluated_constraints: list[ConstraintType] = Field(default_factory=list)
    any_node_schedulable: bool = False
    reasoning: str
    #: Same by-ID-only link pattern as ResourceRecommendation.related_incident_ids
    #: -- never a live object reference.
    related_incident_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

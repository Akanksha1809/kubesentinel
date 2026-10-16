"""Models for the chaos & resilience evaluation harness (Milestone 7 of
docs/PLATFORM_ROADMAP.md). See docs/CHAOS_RESILIENCE_LAB.md for the full
design -- in particular why time_to_detection and time_to_diagnosis are the
same number for every TROUBLESHOOTING scenario (there is no separate
detect-then-diagnose phase in this architecture), why recovery_time_seconds
is None for every scenario except transient-recovery, and why
false_positive_rate is a whole-battery property, never a per-scenario one.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class EvaluationCapability(str, Enum):
    """Which of the six engines owns a given scenario. Capacity planning is
    deliberately not its own value -- it is OPTIMIZATION with
    category=CAPACITY, the same non-duplication this project already
    applies to capacity everywhere else (see docs/CAPACITY_PLANNING.md)."""

    TROUBLESHOOTING = "TROUBLESHOOTING"
    OPTIMIZATION = "OPTIMIZATION"
    SCHEDULING = "SCHEDULING"
    NETWORK = "NETWORK"
    SECURITY = "SECURITY"


class DetectionOutcome(str, Enum):
    DETECTED = "DETECTED"
    #: Something fired for this scenario's own resource, but not the
    #: expected category/reason/classification -- a real finding, just not
    #: the one this scenario was built to produce. Never silently counted
    #: as DETECTED.
    WRONG_CATEGORY = "WRONG_CATEGORY"
    TIMED_OUT = "TIMED_OUT"


class ScenarioResult(BaseModel):
    scenario_name: str
    capability: EvaluationCapability
    applied_at: datetime
    outcome: DetectionOutcome

    #: Seconds from applied_at to the first relevant signal. None only when
    #: outcome is TIMED_OUT.
    time_to_detection_seconds: float | None = None
    #: Equal to time_to_detection_seconds for TROUBLESHOOTING scenarios --
    #: see module docstring. Computed independently anyway, from
    #: diagnosis_history[0] rather than first_observed_at, so a genuine
    #: future gap between the two would actually show up here.
    time_to_diagnosis_seconds: float | None = None
    #: Only ever populated for scenario_name == "transient-recovery" --
    #: None for every other scenario, honestly, since no other scenario
    #: defines a recovery path at all.
    recovery_time_seconds: float | None = None

    #: What was actually observed (a ReasonCode value, a RecommendationCategory
    #: value, a NetworkFindingClassification value, a security rule_id, or
    #: "SCHEDULABLE"/"UNSCHEDULABLE" for the scheduling engine) -- populated
    #: whenever anything was found at all, matching or not.
    observed: str | None = None
    detail: str


class EvaluationRun(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime
    scenario_results: list[ScenarioResult] = Field(default_factory=list)

    #: Whole-battery property (docs/CHAOS_RESILIENCE_LAB.md §5), not a sum
    #: or average over scenario_results.
    false_positive_count: int = 0
    total_open_findings_count: int = 0
    false_positive_rate: float | None = None

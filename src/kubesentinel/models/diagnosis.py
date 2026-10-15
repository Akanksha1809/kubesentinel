"""Diagnosis: the output of a single diagnostic rule, and the ranked result of a run."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INCONCLUSIVE = "inconclusive"


class ReasonCode(str, Enum):
    """Machine-readable root-cause categories. Extended as new rules are added."""

    CRASH_LOOP_BACKOFF = "CRASH_LOOP_BACKOFF"
    OOM_KILLED = "OOM_KILLED"
    IMAGE_PULL_BACKOFF = "IMAGE_PULL_BACKOFF"
    ERR_IMAGE_PULL = "ERR_IMAGE_PULL"
    PENDING_INSUFFICIENT_RESOURCES = "PENDING_INSUFFICIENT_RESOURCES"
    PENDING_SCHEDULING_CONSTRAINT = "PENDING_SCHEDULING_CONSTRAINT"
    READINESS_PROBE_FAILING = "READINESS_PROBE_FAILING"
    LIVENESS_PROBE_FAILING = "LIVENESS_PROBE_FAILING"
    ROLLOUT_FAILED = "ROLLOUT_FAILED"
    UNSATISFIED_REPLICAS = "UNSATISFIED_REPLICAS"
    SERVICE_SELECTOR_MISMATCH = "SERVICE_SELECTOR_MISMATCH"
    DNS_RESOLUTION_FAILURE = "DNS_RESOLUTION_FAILURE"
    PVC_BINDING_FAILURE = "PVC_BINDING_FAILURE"
    NODE_UNSCHEDULABLE = "NODE_UNSCHEDULABLE"
    MISSING_CONFIG_REFERENCE = "MISSING_CONFIG_REFERENCE"
    INCONCLUSIVE = "INCONCLUSIVE"


class Hypothesis(BaseModel):
    """One diagnostic rule's conclusion. Several may coexist for a single incident."""

    rule_name: str
    reason_code: ReasonCode
    confidence: Confidence
    explanation: str
    # Indices/ids into the incident's evidence list that support this hypothesis.
    supporting_evidence: list[str] = Field(default_factory=list)
    # What would raise confidence but wasn't available (e.g. "previous container log unavailable").
    missing_evidence: list[str] = Field(default_factory=list)
    recommended_next_steps: list[str] = Field(default_factory=list)
    auto_remediation_permitted: bool = False


class DiagnosisResult(BaseModel):
    """The ranked output of one diagnostic engine run against one incident's evidence."""

    performed_at: datetime
    # Ranked highest-confidence first. Empty list means fully inconclusive.
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    rules_evaluated: list[str] = Field(default_factory=list)

    @property
    def top_hypothesis(self) -> Hypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None

    @property
    def is_inconclusive(self) -> bool:
        return not self.hypotheses or all(
            h.confidence == Confidence.INCONCLUSIVE for h in self.hypotheses
        )

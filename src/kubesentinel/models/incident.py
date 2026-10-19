"""Incident: the normalized record of one ongoing or resolved workload problem."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from kubesentinel.models.action import RemediationAction
from kubesentinel.models.diagnosis import DiagnosisResult
from kubesentinel.models.evidence import Evidence


class IncidentSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IncidentStatus(str, Enum):
    OPEN = "OPEN"
    DIAGNOSING = "DIAGNOSING"
    DIAGNOSED = "DIAGNOSED"
    REMEDIATION_PLANNED = "REMEDIATION_PLANNED"
    REMEDIATION_IN_PROGRESS = "REMEDIATION_IN_PROGRESS"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    #: The specific Pod this incident was about was replaced by a Deployment
    #: rollout -- confirmed gone (a live 404), with its owning Deployment
    #: confirmed healthy. Deliberately distinct from RESOLVED: no recovery of
    #: *this* object was ever observed, so recovery_duration_seconds is never
    #: set for a SUPERSEDED incident. See docs/INCIDENT_RECONCILIATION_DESIGN.md.
    SUPERSEDED = "SUPERSEDED"


class AuditEntry(BaseModel):
    timestamp: datetime
    event_type: str
    actor: str
    details: dict = Field(default_factory=dict)


class ResourceRef(BaseModel):
    """Identity of the Kubernetes object this incident is about.

    uid is the primary dedup key component: a delete+recreate of the same name gets a
    new UID and therefore a new incident, never silently merged with the old one.
    """

    cluster: str = "default"
    namespace: str
    kind: str
    name: str
    uid: str
    owner_kind: str | None = None
    owner_name: str | None = None
    owner_uid: str | None = None
    # Only ever set for a Pod (from `pod.spec.node_name`, once scheduled) --
    # the stable "slot" a DaemonSet-owned Pod occupies, since its own name is
    # freshly generated on every recreation (unlike a StatefulSet Pod's,
    # which is reused). Lets a stale DaemonSet-owned Pod incident be matched
    # to its replacement by node, the same role a Deployment's ReplicaSet
    # chain plays for that kind. See
    # incidents/manager.py::IncidentManager._maybe_supersede_stale_daemonset_pod_incidents.
    node_name: str | None = None


class Incident(BaseModel):
    incident_id: str
    resource: ResourceRef
    category: str = "UNCLASSIFIED"
    severity: IncidentSeverity = IncidentSeverity.LOW
    status: IncidentStatus = IncidentStatus.OPEN

    first_observed_at: datetime
    last_observed_at: datetime
    resolved_at: datetime | None = None
    recovery_duration_seconds: float | None = None

    evidence: list[Evidence] = Field(default_factory=list)
    diagnosis_history: list[DiagnosisResult] = Field(default_factory=list)
    actions: list[RemediationAction] = Field(default_factory=list)
    audit_log: list[AuditEntry] = Field(default_factory=list)

    # Count of distinct observation windows merged into this incident record.
    occurrence_count: int = 1

    # The category label `incidents_active` currently credits this incident's
    # open slot to, or None while resolved/superseded (not currently counted).
    # Tracked separately from `category` because diagnosis can re-categorize
    # an already-open incident as more evidence arrives (e.g.
    # CRASH_LOOP_BACKOFF -> OOM_KILLED) -- `incidents_active` must be
    # decremented under whichever label it was actually incremented under,
    # never re-derived from `category` at resolve time, or the gauge drifts.
    # See incidents/manager.py::IncidentManager._rebalance_active_metric.
    active_metric_category: str | None = None

    # Consecutive confirmed-healthy observations seen in a row. Resolution requires
    # this to reach a threshold (see incidents/manager.py), not just one healthy
    # snapshot -- a flaky workload can be briefly Ready between crashes. This is a
    # debounce for passive observation, not the windowed Recovery Verifier planned
    # for Milestone 5.
    consecutive_healthy_observations: int = 0

    @property
    def latest_diagnosis(self) -> DiagnosisResult | None:
        return self.diagnosis_history[-1] if self.diagnosis_history else None

    def add_audit(self, event_type: str, actor: str, **details: object) -> None:
        self.audit_log.append(
            AuditEntry(
                timestamp=self.last_observed_at,
                event_type=event_type,
                actor=actor,
                details=details,
            )
        )

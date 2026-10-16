"""Incident lifecycle: dedup ongoing failures, open new incidents, record recovery.

Dedup key is the resource UID, not name/namespace -- deleting and recreating a Pod
with the same name produces a new UID and therefore a new incident, never silently
merged into the old (already-resolved or still-open) one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from kubesentinel.diagnostics.engine import DiagnosticEngine
from kubesentinel.incidents.health import has_failure_signal, is_confirmed_healthy
from kubesentinel.incidents.severity import severity_for
from kubesentinel.metrics.registry import (
    incidents_active,
    incidents_detected_total,
    recovery_duration_seconds,
)
from kubesentinel.models.diagnosis import DiagnosisResult, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence
from kubesentinel.models.incident import Incident, IncidentStatus, ResourceRef
from kubesentinel.persistence.interface import IncidentRepository, OptimizationRepository

logger = logging.getLogger(__name__)

# Cap on retained evidence items per incident, to avoid unbounded growth for a
# long-running incident. Oldest items are dropped first.
MAX_EVIDENCE_PER_INCIDENT = 200

# Consecutive confirmed-healthy observations required before resolving an incident.
# Found necessary live: a flaky workload (e.g. a container that runs for a couple of
# seconds before crashing again) can be legitimately Running+Ready for one watch
# event in between crashes, which would otherwise resolve-then-immediately-reopen the
# same incident. Requiring more than one in a row is a cheap debounce for *passive*
# observation -- it is not a substitute for the windowed, deadline-based Recovery
# Verifier planned for Milestone 5, which verifies an actively-taken remediation
# action rather than debouncing passive incident-manager observations.
RECOVERY_CONFIRMATION_THRESHOLD = 2


class IncidentManager:
    def __init__(
        self,
        repo: IncidentRepository,
        engine: DiagnosticEngine | None = None,
        optimization_repo: OptimizationRepository | None = None,
    ):
        self._repo = repo
        self._engine = engine or DiagnosticEngine()
        # Optional, additive -- every existing caller that only passes `repo`
        # (and possibly `engine`) is unaffected; see _enrich_with_recommendations.
        self._optimization_repo = optimization_repo

    @property
    def engine(self) -> DiagnosticEngine:
        return self._engine

    def reconcile_metrics(self) -> None:
        """Set `incidents_active` from the repository's current state.

        Found necessary live: `incidents_active` is a process-local Prometheus
        Gauge, only ever `.inc()`/`.dec()`'d by `_open_new`/`_resolve`. An
        incident that was already open before this process started -- loaded
        from SQLite, not freshly detected -- never passes through
        `_open_new`, so without this the gauge silently reads zero (or
        undercounts) for every category with incidents that predate the
        current process, even though `/incidents` correctly shows them.
        Call this once, right after construction, before the observer starts
        producing new events.
        """
        # Zero every known category first (not just the ones with open
        # incidents right now): a category that had incidents in a *previous*
        # process which are all resolved by the time this one starts must
        # read an explicit 0, not be left absent from /metrics, which would
        # otherwise look identical to "this category has never happened" to
        # a Grafana panel or an absence-based alert.
        for reason_code in ReasonCode:
            incidents_active.labels(category=reason_code.value).set(0)

        open_incidents = [i for i in self._repo.list() if i.status != IncidentStatus.RESOLVED]
        counts: dict[str, int] = {}
        for incident in open_incidents:
            counts[incident.category] = counts.get(incident.category, 0) + 1
        for category, count in counts.items():
            incidents_active.labels(category=category).set(count)

    def observe(self, resource: ResourceRef, evidence: list[Evidence]) -> Incident | None:
        now = datetime.now(timezone.utc)
        existing = self._repo.find_open_by_resource_uid(resource.uid)

        if existing is not None:
            if is_confirmed_healthy(evidence):
                existing.consecutive_healthy_observations += 1
                existing.last_observed_at = now
                if existing.consecutive_healthy_observations >= RECOVERY_CONFIRMATION_THRESHOLD:
                    self._resolve(existing, now)
                self._repo.save(existing)
                return existing

            existing.consecutive_healthy_observations = 0
            incident = self._update_existing(existing, evidence, now)
            self._repo.save(incident)
            return incident

        if not has_failure_signal(evidence):
            # No open incident and nothing bad observed -- a normal, possibly still
            # starting, Pod. Not confirmed-healthy either, but that distinction only
            # matters for resolving an *existing* incident, not for deciding whether
            # to open a new one.
            return None

        incident = self._open_new(resource, evidence, now)
        self._repo.save(incident)
        return incident

    def _open_new(self, resource: ResourceRef, evidence: list[Evidence], now: datetime) -> Incident:
        incident = Incident(
            incident_id=str(uuid4()),
            resource=resource,
            first_observed_at=now,
            last_observed_at=now,
            evidence=evidence,
            status=IncidentStatus.DIAGNOSING,
        )
        incident.add_audit("incident_opened", actor="observer", resource_uid=resource.uid)
        result = self._engine.diagnose(incident.evidence)
        incident.diagnosis_history.append(result)
        self._apply_diagnosis(incident, result)
        incidents_detected_total.labels(category=incident.category).inc()
        incidents_active.labels(category=incident.category).inc()
        logger.info(
            "opened incident",
            extra={"incident_id": incident.incident_id, "resource": resource.name},
        )
        return incident

    def _update_existing(self, incident: Incident, evidence: list[Evidence], now: datetime) -> Incident:
        incident.last_observed_at = now
        incident.evidence.extend(evidence)
        if len(incident.evidence) > MAX_EVIDENCE_PER_INCIDENT:
            incident.evidence = incident.evidence[-MAX_EVIDENCE_PER_INCIDENT:]

        previous_top = incident.latest_diagnosis.top_hypothesis if incident.latest_diagnosis else None
        result = self._engine.diagnose(incident.evidence)
        new_top = result.top_hypothesis

        materially_changed = not (
            previous_top and new_top and previous_top.reason_code == new_top.reason_code
        )
        if materially_changed:
            incident.diagnosis_history.append(result)
        self._apply_diagnosis(incident, result)
        return incident

    def _apply_diagnosis(self, incident: Incident, result: DiagnosisResult) -> None:
        if result.top_hypothesis:
            incident.category = result.top_hypothesis.reason_code.value
            incident.severity = severity_for(result.top_hypothesis.reason_code)
            incident.status = IncidentStatus.DIAGNOSED
            self._enrich_with_recommendations(incident, result.top_hypothesis)
        else:
            incident.category = "INCONCLUSIVE"
            incident.status = IncidentStatus.DIAGNOSING

    def _enrich_with_recommendations(self, incident: Incident, hypothesis: Hypothesis) -> None:
        """Cross-links the top hypothesis back to any already-persisted
        resource-optimization recommendation for this same workload --
        Milestone 1's own "stabilize the core" audit, item 3. Deliberately
        narrow for its first case: only `OOM_KILLED`, the roadmap's own
        named example ("this container has no memory limit configured, per
        recommendation <id>"), and only `MEMORY`/`DATA_QUALITY` categories
        -- the two that can plausibly explain an OOM kill. Never queries
        anything when no `optimization_repo` was wired up (e.g. in tests
        that only pass `repo`), the same "absence of the check, not a false
        negative" shape `NetworkService`/`SchedulingService` already use
        for their own optional `incident_repo`."""
        if self._optimization_repo is None or hypothesis.reason_code != ReasonCode.OOM_KILLED:
            return
        workload_name = incident.resource.owner_name or incident.resource.name
        recommendation_ids: list[str] = []
        for category in ("MEMORY", "DATA_QUALITY"):
            for recommendation in self._optimization_repo.list_recommendations(
                namespace=incident.resource.namespace, workload_name=workload_name, category=category
            ):
                if recommendation.recommendation_id not in recommendation_ids:
                    recommendation_ids.append(recommendation.recommendation_id)
        hypothesis.related_recommendation_ids = recommendation_ids

    @staticmethod
    def _resolve(incident: Incident, now: datetime) -> None:
        incident.last_observed_at = now
        incident.status = IncidentStatus.RESOLVED
        incident.resolved_at = now
        incident.recovery_duration_seconds = (now - incident.first_observed_at).total_seconds()
        incident.add_audit("incident_resolved", actor="observer")
        incidents_active.labels(category=incident.category).dec()
        recovery_duration_seconds.labels(category=incident.category).observe(
            incident.recovery_duration_seconds
        )
        logger.info(
            "resolved incident",
            extra={
                "incident_id": incident.incident_id,
                "recovery_duration_seconds": incident.recovery_duration_seconds,
            },
        )

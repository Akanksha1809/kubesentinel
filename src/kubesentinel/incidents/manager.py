"""Incident lifecycle: dedup ongoing failures, open new incidents, record recovery.

Dedup key is the resource UID, not name/namespace -- deleting and recreating a Pod
with the same name produces a new UID and therefore a new incident, never silently
merged into the old (already-resolved or still-open) one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from kubernetes.client.rest import ApiException

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
from kubesentinel.observer.client import ClusterClients
from kubesentinel.optimization.ownership import controller_ref
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
        clients: ClusterClients | None = None,
    ):
        self._repo = repo
        self._engine = engine or DiagnosticEngine()
        # Optional, additive -- every existing caller that only passes `repo`
        # (and possibly `engine`) is unaffected; see _enrich_with_recommendations.
        self._optimization_repo = optimization_repo
        # Optional, additive -- every existing caller that doesn't pass this
        # is unaffected (the stale-Pod-incident sweep below becomes a no-op
        # when it's None). This is the one thing IncidentManager uses live
        # cluster access for: confirming a Pod is genuinely gone and its
        # owning ReplicaSet's own controller really is the Deployment that
        # was just observed healthy. See
        # docs/INCIDENT_RECONCILIATION_DESIGN.md and
        # _maybe_supersede_stale_pod_incidents.
        self._clients = clients

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

        open_incidents = [
            i for i in self._repo.list() if i.status not in (IncidentStatus.RESOLVED, IncidentStatus.SUPERSEDED)
        ]
        counts: dict[str, int] = {}
        for incident in open_incidents:
            counts[incident.category] = counts.get(incident.category, 0) + 1
            if incident.active_metric_category != incident.category:
                # Backfills `active_metric_category` for an incident
                # persisted before that field existed (defaults to None),
                # or whose category changed but didn't get saved before a
                # crash -- either way, it must already match `category`
                # before any new `_update_existing`/`_resolve` call runs
                # against it, or `_rebalance_active_metric` would wrongly
                # treat this as a fresh category change and double-count.
                incident.active_metric_category = incident.category
                self._repo.save(incident)
        for category, count in counts.items():
            incidents_active.labels(category=category).set(count)

    def observe(self, resource: ResourceRef, evidence: list[Evidence]) -> Incident | None:
        now = datetime.now(timezone.utc)

        # Fires independent of whether *this* Deployment has an open
        # incident of its own -- the common case is a rollout that never
        # opens one at all, which is exactly the case a stale old Pod
        # incident most needs this sweep for. See
        # docs/INCIDENT_RECONCILIATION_DESIGN.md §3 for why this can't be
        # gated on "an incident just resolved" instead.
        if resource.kind == "Deployment" and is_confirmed_healthy(evidence):
            self._maybe_supersede_stale_pod_incidents(resource, now)

        # Fires on a single DaemonSet-owned Pod's own confirmed-healthy
        # observation, not a controller-level signal -- this project
        # watches no DaemonSet directly (see
        # _maybe_supersede_stale_daemonset_pod_incidents's own docstring
        # for why that's a different trigger shape, not a gap).
        if resource.kind == "Pod" and resource.owner_kind == "DaemonSet" and is_confirmed_healthy(evidence):
            self._maybe_supersede_stale_daemonset_pod_incidents(resource, now)

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
        self._rebalance_active_metric(incident)
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

        # Same reason_code alone isn't "unchanged" -- a rule can raise its
        # own confidence for the identical reason_code as more evidence
        # accumulates (e.g. CrashLoopBackOffRule's MEDIUM -> HIGH once
        # restart_count crosses its own threshold). Found live: dedupe-by-
        # reason_code-only silently discarded that refinement, keeping the
        # log's only entry for this reason_code at its own first, weaker
        # confidence forever. Deliberately not also comparing explanation
        # text or evidence IDs -- those differ on nearly every observation
        # (a restart count ticking up, a fresh evidence UUID) regardless of
        # whether anything about the actual diagnosis is more certain, and
        # diffing them would defeat the log's own "readable, not every
        # observation" purpose.
        materially_changed = not (
            previous_top
            and new_top
            and previous_top.reason_code == new_top.reason_code
            and previous_top.confidence == new_top.confidence
        )
        if materially_changed:
            incident.diagnosis_history.append(result)
        self._apply_diagnosis(incident, result)
        self._rebalance_active_metric(incident)
        return incident

    @staticmethod
    def _rebalance_active_metric(incident: Incident) -> None:
        """Keeps `incidents_active` in sync with `incident.category`, even
        when diagnosis re-categorizes an already-open incident as more
        evidence arrives (e.g. CRASH_LOOP_BACKOFF -> OOM_KILLED). Found
        live: without this, `_open_new` incremented whatever category was
        current at open, and `_resolve` decremented whatever category was
        current at resolve -- if diagnosis changed category in between, the
        gauge drifted: the original category's slot never decremented
        (stuck "active" forever) and the new category's slot decremented
        without this incident ever having incremented it.

        Called on every open-or-still-open path (`_open_new`,
        `_update_existing`), always comparing against
        `active_metric_category` (what the gauge currently credits this
        incident to), never against what it previously credited plus a
        guess -- a no-op when the category hasn't actually changed."""
        previous = incident.active_metric_category
        if previous == incident.category:
            return
        if previous is not None:
            incidents_active.labels(category=previous).dec()
        incidents_active.labels(category=incident.category).inc()
        incident.active_metric_category = incident.category

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
        # Decrement whichever category this incident was actually credited
        # under (`active_metric_category`, kept in sync by
        # `_rebalance_active_metric`), never `incident.category` directly --
        # the two can differ if diagnosis re-categorized this incident while
        # it was open, and decrementing the wrong label is exactly the
        # gauge-drift bug this field exists to prevent. Falls back to
        # `category` only for an incident persisted before this field
        # existed and never rebalanced since (should already be backfilled
        # by `reconcile_metrics` in practice).
        incidents_active.labels(category=incident.active_metric_category or incident.category).dec()
        incident.active_metric_category = None
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

    def _maybe_supersede_stale_pod_incidents(self, deployment_resource: ResourceRef, now: datetime) -> None:
        """Closes a Pod-level incident whose own Pod was replaced by this
        Deployment's rollout -- confirmed live, never guessed. See
        docs/INCIDENT_RECONCILIATION_DESIGN.md for the full design this
        implements; this docstring only tracks the steps, not the
        reasoning behind them.

        No-op when no `clients` was wired up (the same "absence of the
        check, not a false negative" shape `_enrich_with_recommendations`
        already uses for its own optional `optimization_repo`), and
        no-op, with zero live API calls, whenever there is nothing in the
        repository that could possibly be a candidate -- the common case
        once this has been running a while.
        """
        if self._clients is None:
            return

        candidates = [
            i
            for i in self._repo.list(namespace=deployment_resource.namespace)
            if i.resource.kind == "Pod"
            and i.resource.owner_kind == "ReplicaSet"
            and i.status not in (IncidentStatus.RESOLVED, IncidentStatus.SUPERSEDED)
        ]
        if not candidates:
            return

        try:
            replicasets = self._clients.apps.list_namespaced_replica_set(
                namespace=deployment_resource.namespace
            ).items
        except ApiException:
            logger.exception(
                "failed listing ReplicaSets for stale-incident sweep",
                extra={"namespace": deployment_resource.namespace},
            )
            return
        replicasets_by_uid = {rs.metadata.uid: rs for rs in replicasets}

        for incident in candidates:
            replica_set = replicasets_by_uid.get(incident.resource.owner_uid)
            if replica_set is None:
                # Already pruned (revisionHistoryLimit), or never listed --
                # the chain can't be verified, so this incident is left
                # exactly as it is today. See
                # docs/INCIDENT_RECONCILIATION_DESIGN.md Section 6.
                continue
            owner = controller_ref(replica_set.metadata.owner_references)
            if owner is None or owner.kind != "Deployment" or owner.uid != deployment_resource.uid:
                continue

            try:
                self._clients.core.read_namespaced_pod(
                    name=incident.resource.name, namespace=incident.resource.namespace
                )
                # Still exists (even under a different UID, which would be a
                # genuinely different situation this sweep doesn't attempt
                # to classify -- see docs/INCIDENT_RECONCILIATION_DESIGN.md
                # Section 4 step 3) -- leave it alone.
                continue
            except ApiException as exc:
                if exc.status != 404:
                    # Some other error (e.g. a transient 5xx) -- be
                    # conservative, never supersede on an inconclusive check.
                    continue

            self._supersede(
                incident,
                now,
                replaced_by=deployment_resource.name,
                reason="pod confirmed gone (live 404); owning Deployment observed healthy",
            )
            self._repo.save(incident)

    def _maybe_supersede_stale_daemonset_pod_incidents(self, pod_resource: ResourceRef, now: datetime) -> None:
        """The DaemonSet analog of `_maybe_supersede_stale_pod_incidents`,
        triggered differently because this project watches no DaemonSet
        directly (no owning-controller health signal to key off, unlike a
        Deployment's own Available/Progressing conditions): a single
        DaemonSet-owned Pod's own confirmed-healthy observation is the
        trigger instead, scoped to its exact (DaemonSet, node) slot so a
        genuinely still-unhealthy Pod on a *different* node is never
        touched by it.

        Needs no live ReplicaSet-style lookup at all, unlike the Deployment
        case -- a DaemonSet owns its Pods directly, so `owner_uid`, already
        recorded on the incident at open time (`resource_ref_from_pod`
        reads it straight off the Pod's own single ownerReference), is
        already everything needed to find the right candidates. See
        docs/INCIDENT_RECONCILIATION_DESIGN.md Section 8.

        Node-scoped, not just DaemonSet-scoped: a DaemonSet-owned Pod's own
        name is freshly generated on every recreation (like a Deployment's,
        unlike a StatefulSet's), so there is no name to confirm-via-404
        across a recreation without first finding the right candidate --
        `node_name` is the stable identifier that plays the role a
        Deployment's ReplicaSet chain plays for that kind."""
        if self._clients is None:
            return
        if pod_resource.owner_kind != "DaemonSet" or pod_resource.owner_uid is None or pod_resource.node_name is None:
            return

        candidates = [
            i
            for i in self._repo.list(namespace=pod_resource.namespace)
            if i.resource.kind == "Pod"
            and i.resource.owner_kind == "DaemonSet"
            and i.resource.owner_uid == pod_resource.owner_uid
            and i.resource.node_name == pod_resource.node_name
            and i.resource.uid != pod_resource.uid
            and i.status not in (IncidentStatus.RESOLVED, IncidentStatus.SUPERSEDED)
        ]
        if not candidates:
            return

        for incident in candidates:
            try:
                self._clients.core.read_namespaced_pod(
                    name=incident.resource.name, namespace=incident.resource.namespace
                )
                # Still exists (even under a different UID) -- leave it
                # alone, same as the Deployment case.
                continue
            except ApiException as exc:
                if exc.status != 404:
                    continue

            self._supersede(
                incident,
                now,
                replaced_by=pod_resource.owner_name or incident.resource.owner_name or "<unknown-daemonset>",
                reason="pod confirmed gone (live 404); a healthy replacement pod observed on the same node",
            )
            self._repo.save(incident)

    @staticmethod
    def _supersede(incident: Incident, now: datetime, replaced_by: str, reason: str) -> None:
        """Closes an incident whose own Pod was replaced -- deliberately not
        `_resolve()`: no recovery of *this* object was ever observed, so
        `recovery_duration_seconds` is never set here. See
        docs/INCIDENT_RECONCILIATION_DESIGN.md Section 1."""
        incident.last_observed_at = now
        incident.status = IncidentStatus.SUPERSEDED
        incident.resolved_at = now
        incident.add_audit(
            "incident_superseded",
            actor="observer",
            replaced_by=replaced_by,
            reason=reason,
        )
        # Same `active_metric_category`-not-`category` rule `_resolve` uses,
        # and for the same reason -- see its own comment.
        incidents_active.labels(category=incident.active_metric_category or incident.category).dec()
        incident.active_metric_category = None
        logger.info(
            "superseded incident",
            extra={"incident_id": incident.incident_id, "replaced_by": replaced_by},
        )

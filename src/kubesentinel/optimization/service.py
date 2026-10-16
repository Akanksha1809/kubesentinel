"""Orchestrates inventory -> analyzer -> safety -> persistence for the
optimization engine. The one entry point the API/CLI call through -- mirrors
`remediation/service.py::RemediationService`'s role for the troubleshooting
engine, including its "this module is where side effects start" shape:
everything upstream (`inventory.py`, `analyzer.py`, `safety.py`) stays pure/
read-only, and this is the only place that writes to the repository.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from kubesentinel.config import Settings
from kubesentinel.metrics.registry import (
    optimization_analysis_total,
    optimization_last_success_timestamp,
    optimization_metrics_data_available,
    optimization_recommendations_total,
)
from kubesentinel.models.optimization import (
    AnalysisRunSummary,
    RecommendationStatus,
    ResourceRecommendation,
    WorkloadSnapshot,
)
from kubesentinel.observer.client import ClusterClients
from kubesentinel.optimization.analyzer import analyze_workload
from kubesentinel.optimization.capacity import analyze_capacity, attach_hpa_snapshots, build_hpa_inventory
from kubesentinel.optimization.inventory import build_workload_inventory
from kubesentinel.optimization.metrics_providers import (
    InMemorySampleStore,
    MetricsApiProvider,
    MetricsProviderError,
    RawUsageSample,
)
from kubesentinel.optimization.safety import apply_safety_checks
from kubesentinel.persistence.interface import IncidentRepository, OptimizationRepository
from kubesentinel.scheduling.node_inventory import build_node_inventory

logger = logging.getLogger(__name__)

_REVIEW_DECISIONS = (RecommendationStatus.REVIEWED, RecommendationStatus.APPROVED, RecommendationStatus.REJECTED)


class RecommendationNotFoundError(Exception):
    pass


class InvalidRecommendationStateError(Exception):
    """Raised when a review doesn't match the recommendation's current
    (possibly effectively-expired) state."""


def _with_effective_status(recommendation: ResourceRecommendation, now: datetime) -> ResourceRecommendation:
    """Computes the status as of `now` without ever mutating storage just by
    reading it: a `NEW`/`REVIEWED`/`APPROVED` recommendation past its
    `expires_at` reads as `EXPIRED` here, even if nothing has re-run
    `analyze()` to persist that -- "a GET must say EXPIRED rather than
    silently serving a stale number," per the spec, without needing a
    background job to flip stored rows on a timer."""
    if (
        recommendation.status in (RecommendationStatus.NEW, RecommendationStatus.REVIEWED, RecommendationStatus.APPROVED)
        and now > recommendation.expires_at
    ):
        return recommendation.model_copy(update={"status": RecommendationStatus.EXPIRED})
    return recommendation


class OptimizationService:
    def __init__(
        self,
        clients: ClusterClients,
        repo: OptimizationRepository,
        settings: Settings,
        incident_repo: IncidentRepository | None = None,
    ):
        self._clients = clients
        self._repo = repo
        self._settings = settings
        self._incident_repo = incident_repo
        self._sample_store = InMemorySampleStore(max_samples_per_key=settings.optimization_sample_store_max_samples)
        self._metrics_provider = MetricsApiProvider(clients.custom_objects, settings.optimization_metrics_timeout_seconds)

    def analyze(self, namespaces: list[str] | None = None) -> AnalysisRunSummary:
        now = datetime.now(timezone.utc)
        namespaces = namespaces or []
        try:
            workloads = build_workload_inventory(
                self._clients,
                namespaces=namespaces,
                excluded_namespaces=self._settings.optimization_excluded_namespace_list,
            )

            raw_samples, metrics_available, warning = self._collect_raw_samples(namespaces)

            hpas = build_hpa_inventory(self._clients, namespaces=namespaces)
            attach_hpa_snapshots(workloads, hpas)
            # Real Node allocatable-vs-requested totals, reused from the
            # scheduling advisor (see docs/CAPACITY_PLANNING.md §1) for the
            # one scale-headroom check analyze_capacity() needs. Never
            # fails the whole analyze() pass if this one read fails (e.g. a
            # transient Node-list error) -- the headroom check simply
            # degrades to "unknown" rather than taking down every other
            # finding this pass would otherwise produce.
            try:
                node_snapshots = build_node_inventory(self._clients)
            except Exception as exc:  # noqa: BLE001 - degrade, never abort the whole analyze() pass for this
                logger.info("node inventory unavailable for this analyze() pass's capacity headroom check: %s", exc)
                node_snapshots = []

            all_recommendations: list[ResourceRecommendation] = []
            for workload in workloads:
                related_incident_ids = self._open_incident_ids_for(workload)
                all_recommendations.extend(
                    analyze_workload(
                        workload,
                        raw_samples,
                        self._sample_store,
                        self._settings,
                        now=now,
                        related_incident_ids=related_incident_ids,
                    )
                )
                capacity_recommendation = analyze_capacity(workload, raw_samples, node_snapshots, self._settings, now=now)
                if capacity_recommendation is not None:
                    all_recommendations.extend(
                        apply_safety_checks(workload, [capacity_recommendation], self._settings, related_incident_ids)
                    )

            self._persist(workloads, all_recommendations)
        except Exception:
            optimization_analysis_total.labels(status="failed").inc()
            raise

        optimization_analysis_total.labels(status="success" if metrics_available else "partial").inc()
        by_category: dict[str, int] = {}
        for recommendation in all_recommendations:
            by_category[recommendation.category.value] = by_category.get(recommendation.category.value, 0) + 1
        for category, count in by_category.items():
            optimization_recommendations_total.labels(category=category).inc(count)
        optimization_last_success_timestamp.set(now.timestamp())
        optimization_metrics_data_available.labels(provider="metrics_api").set(1 if metrics_available else 0)
        optimization_metrics_data_available.labels(provider="prometheus").set(
            1 if self._settings.optimization_prometheus_url else 0
        )

        summary = AnalysisRunSummary(
            analyzed_at=now,
            namespaces=namespaces,
            workload_count=len(workloads),
            recommendation_count=len(all_recommendations),
            metrics_available=metrics_available,
            warning=warning,
        )
        self._repo.save_analysis_run(summary)
        return summary

    def _open_incident_ids_for(self, workload: WorkloadSnapshot) -> list[str]:
        """Checks the workload's own UID and every current Pod's UID for a
        currently-open (non-resolved) incident -- an incident in this project
        can be opened against either, depending on which diagnostic rule
        found it (e.g. a Pod-level CrashLoopBackOff vs. a Deployment-level
        UNSATISFIED_REPLICAS). Returns `[]`, never `None`, when there's no
        incident repository wired up (e.g. in a test or CLI context that
        doesn't have one) -- absence of the check, not a false negative."""
        if self._incident_repo is None:
            return []
        # A standalone Pod's workload_uid is the same UID as its one entry
        # in pod_uids -- dedupe before querying so that case doesn't look up
        # the same UID twice.
        uids = dict.fromkeys((workload.workload_uid, *workload.pod_uids))
        incident_ids: list[str] = []
        for uid in uids:
            incident = self._incident_repo.find_open_by_resource_uid(uid)
            if incident is not None and incident.incident_id not in incident_ids:
                incident_ids.append(incident.incident_id)
        return incident_ids

    def _collect_raw_samples(self, namespaces: list[str]) -> tuple[list[RawUsageSample], bool, str | None]:
        try:
            if namespaces:
                samples: list[RawUsageSample] = []
                for namespace in namespaces:
                    samples.extend(self._metrics_provider.get_current_pod_usage(namespace=namespace))
                return samples, True, None
            return self._metrics_provider.get_current_pod_usage(namespace=None), True, None
        except MetricsProviderError as exc:
            logger.info("metrics provider unavailable for this analyze() pass: %s", exc)
            return [], False, str(exc)

    def _persist(self, workloads: list[WorkloadSnapshot], recommendations: list[ResourceRecommendation]) -> None:
        self._repo.save_workload_snapshots(workloads)
        for recommendation in recommendations:
            # Supersede any still-undecided prior recommendation for the same
            # (namespace, workload, container, category) -- never touches
            # one a human has already reviewed/approved/rejected; that
            # decision is a historical record, not something a later
            # analyze() pass gets to silently overwrite.
            prior = self._repo.list_recommendations(
                namespace=recommendation.namespace,
                workload_name=recommendation.workload_name,
                category=recommendation.category.value,
                status=RecommendationStatus.NEW.value,
            )
            for old in prior:
                if old.container_name == recommendation.container_name and old.recommendation_id != recommendation.recommendation_id:
                    old.status = RecommendationStatus.EXPIRED
                    self._repo.save_recommendation(old)
            self._repo.save_recommendation(recommendation)

    def get_recommendation(self, recommendation_id: str) -> ResourceRecommendation:
        stored = self._repo.get_recommendation(recommendation_id)
        if stored is None:
            raise RecommendationNotFoundError(f"no recommendation {recommendation_id}")
        return _with_effective_status(stored, datetime.now(timezone.utc))

    def list_recommendations(
        self,
        *,
        namespace: str | None = None,
        workload_name: str | None = None,
        category: str | None = None,
        status: str | None = None,
    ) -> list[ResourceRecommendation]:
        now = datetime.now(timezone.utc)
        stored = self._repo.list_recommendations(
            namespace=namespace, workload_name=workload_name, category=category, status=None
        )
        effective = [_with_effective_status(r, now) for r in stored]
        # The `status` filter (if given) applies to the *effective*, not
        # stored, status -- a caller filtering for `status=EXPIRED` must see
        # recommendations that have aged out even if nothing has persisted
        # that yet.
        if status:
            effective = [r for r in effective if r.status.value == status]
        return effective

    def list_workloads(self, namespace: str | None = None) -> list[WorkloadSnapshot]:
        return self._repo.list_workload_snapshots(namespace=namespace)

    def summary(self) -> dict:
        now = datetime.now(timezone.utc)
        recommendations = [_with_effective_status(r, now) for r in self._repo.list_recommendations()]
        by_category: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for recommendation in recommendations:
            by_category[recommendation.category.value] = by_category.get(recommendation.category.value, 0) + 1
            by_status[recommendation.status.value] = by_status.get(recommendation.status.value, 0) + 1
        latest_run = self._repo.get_latest_analysis_run()
        return {
            "total_recommendations": len(recommendations),
            "by_category": by_category,
            "by_status": by_status,
            "latest_analysis": latest_run.model_dump(mode="json") if latest_run else None,
        }

    def review(
        self, recommendation_id: str, decision: RecommendationStatus, reviewed_by: str, notes: str | None = None
    ) -> ResourceRecommendation:
        if decision not in _REVIEW_DECISIONS:
            raise InvalidRecommendationStateError(
                f"{decision.value} is not a valid review decision -- must be one of "
                f"{[d.value for d in _REVIEW_DECISIONS]}"
            )
        stored = self._repo.get_recommendation(recommendation_id)
        if stored is None:
            raise RecommendationNotFoundError(f"no recommendation {recommendation_id}")

        now = datetime.now(timezone.utc)
        effective = _with_effective_status(stored, now)
        if effective.status == RecommendationStatus.EXPIRED:
            raise InvalidRecommendationStateError(
                f"recommendation {recommendation_id} is EXPIRED (as of {stored.expires_at.isoformat()}) -- "
                "re-run analyze() to get a current one before reviewing it; never act on stale evidence"
            )
        if stored.status not in (RecommendationStatus.NEW, RecommendationStatus.REVIEWED):
            raise InvalidRecommendationStateError(
                f"recommendation {recommendation_id} is already {stored.status.value} -- cannot review again"
            )

        stored.status = decision
        stored.reviewed_by = reviewed_by
        stored.reviewed_at = now
        stored.review_notes = notes
        self._repo.save_recommendation(stored)
        return stored

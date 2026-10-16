"""Repository interface for incident persistence.

Kept storage-agnostic so SQLite (the default, zero-ops local store) can be swapped for
PostgreSQL later without touching the incident manager, API, or CLI.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from datetime import datetime

from kubesentinel.models.action import RemediationAction
from kubesentinel.models.evaluation import EvaluationRun
from kubesentinel.models.incident import Incident
from kubesentinel.models.network import NetworkFinding
from kubesentinel.models.optimization import AnalysisRunSummary, ResourceRecommendation, WorkloadSnapshot
from kubesentinel.models.scheduling import SchedulingFinding
from kubesentinel.models.security import SecurityFinding


class IncidentRepository(ABC):
    @abstractmethod
    def get(self, incident_id: str) -> Incident | None: ...

    @abstractmethod
    def find_open_by_resource_uid(self, resource_uid: str) -> Incident | None:
        """Find a non-resolved incident already tracking this exact resource UID."""
        ...

    @abstractmethod
    def save(self, incident: Incident) -> None:
        """Upsert. Must be atomic: concurrent saves of the same incident_id never corrupt state."""
        ...

    @abstractmethod
    def list(
        self, *, status: str | None = None, namespace: str | None = None
    ) -> list[Incident]: ...

    @abstractmethod
    def list_actions_since(self, since: datetime) -> builtins.list[RemediationAction]:
        """Every RemediationAction proposed at or after `since`, across all incidents.

        Used for the policy engine's per-window action budget -- a global rate
        limit independent of which incident each action belongs to.

        Return type is spelled `builtins.list[...]` rather than `list[...]` --
        this class already defines a method named `list` above, which shadows the
        builtin name within this class body's annotation scope.
        """
        ...


class OptimizationRepository(ABC):
    """Persistence for the resource optimization engine -- a separate
    interface from `IncidentRepository`, not merged into it: a
    `ResourceRecommendation` is a different aggregate with a different
    lifecycle, and a `WorkloadSnapshot` has no equivalent in the
    troubleshooting engine at all. Implementations may share the same
    underlying SQLite file as `IncidentRepository` (see
    `persistence/optimization_repo.py`); this interface makes no assumption
    either way.
    """

    @abstractmethod
    def get_recommendation(self, recommendation_id: str) -> ResourceRecommendation | None: ...

    @abstractmethod
    def save_recommendation(self, recommendation: ResourceRecommendation) -> None:
        """Upsert by `recommendation_id`."""
        ...

    @abstractmethod
    def list_recommendations(
        self,
        *,
        namespace: str | None = None,
        workload_name: str | None = None,
        category: str | None = None,
        status: str | None = None,
    ) -> builtins.list[ResourceRecommendation]: ...

    @abstractmethod
    def save_workload_snapshots(self, snapshots: builtins.list[WorkloadSnapshot]) -> None:
        """Replaces the *entire* stored set with `snapshots` -- always the
        full current inventory from one `analyze()` pass, never a partial
        merge, so a workload that disappeared (deleted, or its last Pod
        gone) is never left behind as a stale `GET /optimization/workloads`
        result."""
        ...

    @abstractmethod
    def list_workload_snapshots(self, *, namespace: str | None = None) -> builtins.list[WorkloadSnapshot]: ...

    @abstractmethod
    def save_analysis_run(self, summary: AnalysisRunSummary) -> None:
        """Replaces the previously-saved run -- only the latest is kept,
        this is a freshness marker, not a history log (see
        `models.optimization.AnalysisRunSummary`)."""
        ...

    @abstractmethod
    def get_latest_analysis_run(self) -> AnalysisRunSummary | None: ...


class SchedulingRepository(ABC):
    """Persistence for the scheduling & placement advisor -- a sibling of
    `OptimizationRepository`, not merged into it: `SchedulingFinding` has
    no concept of `review()`/approval the way a recommendation does (this
    engine never proposes anything to accept or reject, only explains).
    Implementations may share the same underlying SQLite file as the other
    two repositories; this interface makes no assumption either way.
    """

    @abstractmethod
    def get_finding(self, finding_id: str) -> SchedulingFinding | None: ...

    @abstractmethod
    def save_finding(self, finding: SchedulingFinding) -> None:
        """Upsert by `finding_id`."""
        ...

    @abstractmethod
    def list_findings(
        self, *, namespace: str | None = None, schedulable: bool | None = None
    ) -> builtins.list[SchedulingFinding]: ...


class NetworkRepository(ABC):
    """Persistence for the network troubleshooting engine -- a sibling of
    `SchedulingRepository`, same reasoning: `NetworkFinding` has no
    review/approval lifecycle either.
    """

    @abstractmethod
    def get_finding(self, finding_id: str) -> NetworkFinding | None: ...

    @abstractmethod
    def save_finding(self, finding: NetworkFinding) -> None:
        """Upsert by `finding_id`."""
        ...

    @abstractmethod
    def list_findings(
        self, *, namespace: str | None = None, classification: str | None = None
    ) -> builtins.list[NetworkFinding]: ...


class SecurityRepository(ABC):
    """Persistence for the security & configuration auditor.

    Unlike `NetworkRepository`/`SchedulingRepository` (one `analyze_pod()`
    call, one finding, so an upsert-by-`finding_id` makes sense),
    `SecurityService.audit()` re-evaluates the *entire* cluster on every
    call -- every finding gets a fresh `finding_id` each run (there is no
    stable identity to upsert against: a rule firing again on the same
    object is not "the same finding," it's "still true"). `save_findings`
    is therefore whole-run replace-not-append, the same semantics
    `optimization_repo.py::save_workload_snapshots` already uses for its
    own full-cluster-rescan shape -- found live, not assumed: an earlier
    per-finding upsert design left two full audit runs' worth of findings
    (several stale) sitting in the table together before this was caught
    and fixed.
    """

    @abstractmethod
    def get_finding(self, finding_id: str) -> SecurityFinding | None: ...

    @abstractmethod
    def save_findings(self, findings: builtins.list[SecurityFinding]) -> None:
        """Replaces every previously-persisted finding with this run's
        own set -- never an incremental upsert."""
        ...

    @abstractmethod
    def list_findings(
        self, *, namespace: str | None = None, severity: str | None = None, rule_id: str | None = None
    ) -> builtins.list[SecurityFinding]: ...


class EvaluationRepository(ABC):
    """Persistence for the chaos & resilience evaluation harness
    (Milestone 7). A sixth sibling, same "may share the same underlying
    SQLite file" note as every repository above -- an `EvaluationRun` has
    no review/approval lifecycle either, and (like `SecurityFinding`) is
    a whole-run record, not an upsert target: a `run_id` is generated once
    per harness invocation and never reused.
    """

    @abstractmethod
    def save_run(self, run: EvaluationRun) -> None: ...

    @abstractmethod
    def get_run(self, run_id: str) -> EvaluationRun | None: ...

    @abstractmethod
    def list_runs(self) -> builtins.list[EvaluationRun]: ...

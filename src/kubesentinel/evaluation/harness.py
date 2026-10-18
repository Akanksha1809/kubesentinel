"""The chaos & resilience evaluation harness itself. See
docs/CHAOS_RESILIENCE_LAB.md for the full design -- this module is the
implementation of §§3, 4, 5, and 6 of that doc.

Deliberately never touches the cluster through `ClusterClients` for
applying/cleaning up a scenario (§6): that's lab tooling, reusing exactly
what `lab/scripts/run-scenario.sh`/`cleanup.sh` already do via a `kubectl`
subprocess, under whatever kubeconfig is ambient. Everything that *reads*
cluster/engine state goes through the same services/repositories every CLI
command already uses.
"""

from __future__ import annotations

import logging
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kubesentinel.config import Settings
from kubesentinel.diagnostics.engine import DiagnosticEngine
from kubesentinel.evaluation.scenarios import SCENARIOS, ScenarioSpec
from kubesentinel.incidents.manager import IncidentManager
from kubesentinel.models.evaluation import DetectionOutcome, EvaluationCapability, EvaluationRun, ScenarioResult
from kubesentinel.network.service import NetworkService, PodNotFoundError as NetworkPodNotFoundError
from kubesentinel.observer.client import build_clients
from kubesentinel.observer.service import ObserverService
from kubesentinel.optimization.service import OptimizationService
from kubesentinel.persistence.interface import EvaluationRepository
from kubesentinel.persistence.network_repo import SQLiteNetworkRepository
from kubesentinel.persistence.optimization_repo import SQLiteOptimizationRepository
from kubesentinel.persistence.scheduling_repo import SQLiteSchedulingRepository
from kubesentinel.persistence.security_repo import SQLiteSecurityRepository
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from kubesentinel.scheduling.service import PodNotFoundError as SchedulingPodNotFoundError, SchedulingService
from kubesentinel.security.service import SecurityService

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 3.0
_DELETE_CONFIRM_TIMEOUT_SECONDS = 30.0
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LAB_ROOT = _REPO_ROOT / "lab"


def _scenario_label(lab_dir: str) -> str:
    return lab_dir.removeprefix("scenario-")


def _run_kubectl(*args: str) -> None:
    subprocess.run(["kubectl", *args], check=True, capture_output=True, text=True)


def _apply_scenario(spec: ScenarioSpec) -> None:
    manifest_dir = _LAB_ROOT / "manifests" / spec.lab_dir
    _run_kubectl("apply", "-f", str(manifest_dir))


def _cleanup_scenario(spec: ScenarioSpec) -> None:
    label = f"kubesentinel.io/scenario={_scenario_label(spec.lab_dir)}"
    _run_kubectl(
        "delete",
        "deployment,pod,pvc,service,networkpolicy,horizontalpodautoscaler,serviceaccount",
        "-n",
        spec.namespace,
        "-l",
        label,
        "--ignore-not-found",
    )
    _run_kubectl("delete", "clusterrolebinding,clusterrole", "-l", label, "--ignore-not-found")


def _confirm_absent(spec: ScenarioSpec, clients) -> None:
    deadline = time.monotonic() + _DELETE_CONFIRM_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        pods = clients.core.list_namespaced_pod(
            namespace=spec.namespace, label_selector=f"kubesentinel.io/scenario={_scenario_label(spec.lab_dir)}"
        ).items
        if not pods:
            return
        time.sleep(1.0)
    logger.warning("scenario '%s' resources did not disappear within %ss", spec.name, _DELETE_CONFIRM_TIMEOUT_SECONDS)


def _resolve_pod_name(clients, namespace: str, app_label: str) -> str | None:
    pods = clients.core.list_namespaced_pod(namespace=namespace, label_selector=f"app={app_label}").items
    return pods[0].metadata.name if pods else None


@dataclass
class _CheckResult:
    outcome: DetectionOutcome
    observed: str | None
    detail: str
    detected_at: float | None  # time.monotonic() timestamp, for elapsed-seconds math
    diagnosed_at: float | None = None
    recovery_seconds: float | None = None


class EvaluationHarness:
    """Owns its own, short-lived `ClusterClients`/repositories/services --
    never the long-running app's own instances -- so a harness run never
    depends on, or interferes with the lifecycle of, a separately-running
    API server process (docs/CHAOS_RESILIENCE_LAB.md §6)."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._clients = build_clients(settings)
        self._incident_repo = SQLiteIncidentRepository(settings.db_path)
        self._optimization_repo = SQLiteOptimizationRepository(settings.db_path)
        self._scheduling_repo = SQLiteSchedulingRepository(settings.db_path)
        self._network_repo = SQLiteNetworkRepository(settings.db_path)
        self._security_repo = SQLiteSecurityRepository(settings.db_path)

    def run(self, scenario_names: list[str]) -> EvaluationRun:
        specs = [s for s in SCENARIOS if s.name in scenario_names] if scenario_names else list(SCENARIOS)
        started_at = datetime.now(timezone.utc)

        # Dedupe by lab_dir: pending-resources-scheduling-fit shares a
        # manifest with pending-resources -- apply/clean it up once, run
        # every spec that targets it against that single applied instance.
        by_dir: dict[str, list[ScenarioSpec]] = {}
        for spec in specs:
            by_dir.setdefault(spec.lab_dir, []).append(spec)

        results: list[ScenarioResult] = []
        for lab_dir, dir_specs in by_dir.items():
            results.extend(self._run_one_scenario_dir(lab_dir, dir_specs))

        false_positive_count, total_open = self._count_false_positives()
        finished_at = datetime.now(timezone.utc)
        run = EvaluationRun(
            run_id=str(uuid.uuid4()),
            started_at=started_at,
            finished_at=finished_at,
            scenario_results=results,
            false_positive_count=false_positive_count,
            total_open_findings_count=total_open,
            false_positive_rate=(false_positive_count / total_open) if total_open else None,
        )
        return run

    def _run_one_scenario_dir(self, lab_dir: str, dir_specs: list[ScenarioSpec]) -> list[ScenarioResult]:
        representative = dir_specs[0]
        _cleanup_scenario(representative)
        _confirm_absent(representative, self._clients)

        applied_at_wall = datetime.now(timezone.utc)
        applied_at_mono = time.monotonic()
        _apply_scenario(representative)

        results = []
        for spec in dir_specs:
            check = self._check_scenario(spec, applied_at_mono)
            results.append(
                ScenarioResult(
                    scenario_name=spec.name,
                    capability=spec.capability,
                    applied_at=applied_at_wall,
                    outcome=check.outcome,
                    time_to_detection_seconds=(
                        (check.detected_at - applied_at_mono) if check.detected_at is not None else None
                    ),
                    time_to_diagnosis_seconds=(
                        (check.diagnosed_at - applied_at_mono) if check.diagnosed_at is not None else None
                    ),
                    recovery_time_seconds=check.recovery_seconds,
                    observed=check.observed,
                    detail=check.detail,
                )
            )
        return results

    def _check_scenario(self, spec: ScenarioSpec, applied_at_mono: float) -> _CheckResult:
        if spec.capability == EvaluationCapability.TROUBLESHOOTING:
            return self._check_troubleshooting(spec, applied_at_mono)
        if spec.capability == EvaluationCapability.OPTIMIZATION:
            return self._check_optimization(spec, applied_at_mono)
        if spec.capability == EvaluationCapability.SCHEDULING:
            return self._check_scheduling(spec, applied_at_mono)
        if spec.capability == EvaluationCapability.NETWORK:
            return self._check_network(spec, applied_at_mono)
        return self._check_security(spec, applied_at_mono)

    # -- TROUBLESHOOTING: push-based, via a harness-owned ObserverService --

    def _check_troubleshooting(self, spec: ScenarioSpec, applied_at_mono: float) -> _CheckResult:
        manager = IncidentManager(
            self._incident_repo,
            engine=DiagnosticEngine(),
            optimization_repo=self._optimization_repo,
            clients=self._clients,
        )
        observer = ObserverService(self._clients, self._settings, manager)
        observer.start()
        try:
            deadline = applied_at_mono + spec.timeout_seconds
            matched_incident = None
            best_candidate = None
            while time.monotonic() < deadline:
                # A single scenario can open *more than one* real incident --
                # found live, not assumed: scenario-failed-mount opens both a
                # Deployment-level UNSATISFIED_REPLICAS incident (the
                # Deployment's own name exactly equals resource_name_prefix)
                # and a Pod-level FAILED_MOUNT one (the Pod's randomly-
                # suffixed name also starts with that same prefix). Taking
                # whichever one the repository happened to list first, the
                # instant either one existed, made this report a false
                # WRONG_CATEGORY for a scenario that was actually correctly
                # diagnosed moments later. Keep polling for an exact match
                # against `expected` until the deadline; only settle for the
                # best (first-seen) non-matching candidate once time runs out,
                # for an honest WRONG_CATEGORY/TIMED_OUT detail.
                for incident in self._incident_repo.list(namespace=spec.namespace):
                    if not incident.resource.name.startswith(spec.resource_name_prefix):
                        continue
                    if best_candidate is None:
                        best_candidate = incident
                    top = incident.latest_diagnosis.top_hypothesis if incident.latest_diagnosis else None
                    observed = top.reason_code.value if top else incident.category
                    if observed == spec.expected:
                        matched_incident = incident
                        break
                if matched_incident is not None:
                    break
                time.sleep(_POLL_INTERVAL_SECONDS)
            if matched_incident is None:
                matched_incident = best_candidate

            if matched_incident is None:
                return _CheckResult(
                    outcome=DetectionOutcome.TIMED_OUT,
                    observed=None,
                    detail=f"no incident for a resource named like '{spec.resource_name_prefix}*' within {spec.timeout_seconds}s",
                    detected_at=None,
                )

            top = matched_incident.latest_diagnosis.top_hypothesis if matched_incident.latest_diagnosis else None
            observed = top.reason_code.value if top else matched_incident.category
            # first_observed_at/diagnosis_history[0].performed_at are real
            # wall-clock timestamps from the watcher thread, not from this
            # method's own monotonic clock, so "now" is used instead --
            # the poll loop above already bounds this to within one
            # _POLL_INTERVAL_SECONDS of the real detection instant. Both are
            # the same value, by construction (docs/CHAOS_RESILIENCE_LAB.md
            # §3: observe() diagnoses inline, there is no separate phase).
            detected_at = time.monotonic()
            diagnosed_at = detected_at

            recovery_seconds = None
            if spec.supports_recovery:
                recovery_seconds = self._wait_for_resolution(matched_incident.incident_id, deadline)

            if observed == spec.expected:
                return _CheckResult(
                    outcome=DetectionOutcome.DETECTED,
                    observed=observed,
                    detail=f"incident {matched_incident.incident_id} diagnosed {observed}",
                    detected_at=detected_at,
                    diagnosed_at=diagnosed_at,
                    recovery_seconds=recovery_seconds,
                )
            return _CheckResult(
                outcome=DetectionOutcome.WRONG_CATEGORY,
                observed=observed,
                detail=(
                    f"incident {matched_incident.incident_id} opened, but top hypothesis was "
                    f"'{observed}', expected '{spec.expected}'"
                ),
                detected_at=detected_at,
                diagnosed_at=diagnosed_at,
                recovery_seconds=recovery_seconds,
            )
        finally:
            observer.stop()

    def _wait_for_resolution(self, incident_id: str, deadline: float) -> float | None:
        while time.monotonic() < deadline:
            incident = self._incident_repo.get(incident_id)
            if incident is not None and incident.resolved_at is not None:
                return incident.recovery_duration_seconds
            time.sleep(_POLL_INTERVAL_SECONDS)
        return None

    # -- OPTIMIZATION: pull-based, re-triggering analyze() each attempt --

    def _check_optimization(self, spec: ScenarioSpec, applied_at_mono: float) -> _CheckResult:
        service = OptimizationService(self._clients, self._optimization_repo, self._settings, incident_repo=self._incident_repo)
        deadline = applied_at_mono + spec.timeout_seconds
        while time.monotonic() < deadline:
            service.analyze(namespaces=[spec.namespace])
            for rec in self._optimization_repo.list_recommendations(
                namespace=spec.namespace, workload_name=spec.resource_name_prefix
            ):
                if rec.category.value != spec.expected:
                    continue
                if spec.required_flag_substring and not any(
                    spec.required_flag_substring in flag for flag in rec.safety_flags
                ):
                    continue
                return _CheckResult(
                    outcome=DetectionOutcome.DETECTED,
                    observed=rec.category.value,
                    detail=f"recommendation {rec.recommendation_id}: {rec.category.value}, flags={rec.safety_flags}",
                    detected_at=time.monotonic(),
                    diagnosed_at=time.monotonic(),
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
        existing = self._optimization_repo.list_recommendations(namespace=spec.namespace, workload_name=spec.resource_name_prefix)
        if existing:
            return _CheckResult(
                outcome=DetectionOutcome.WRONG_CATEGORY,
                observed=existing[0].category.value,
                detail=f"recommendation(s) exist ({[r.category.value for r in existing]}) but none matched expected '{spec.expected}'",
                detected_at=None,
            )
        return _CheckResult(
            outcome=DetectionOutcome.TIMED_OUT,
            observed=None,
            detail=f"no recommendation for workload '{spec.resource_name_prefix}' within {spec.timeout_seconds}s",
            detected_at=None,
        )

    # -- SCHEDULING / NETWORK: pull-based, one real Pod analyzed by name --

    def _check_scheduling(self, spec: ScenarioSpec, applied_at_mono: float) -> _CheckResult:
        service = SchedulingService(self._clients, self._scheduling_repo, incident_repo=self._incident_repo)
        deadline = applied_at_mono + spec.timeout_seconds
        while time.monotonic() < deadline:
            pod_name = _resolve_pod_name(self._clients, spec.namespace, spec.pod_label_app or "")
            if pod_name is not None:
                try:
                    finding = service.analyze_pod(spec.namespace, pod_name)
                except SchedulingPodNotFoundError:
                    finding = None
                if finding is not None:
                    observed = "SCHEDULABLE" if finding.any_node_schedulable else "UNSCHEDULABLE"
                    if observed == spec.expected:
                        return _CheckResult(
                            outcome=DetectionOutcome.DETECTED,
                            observed=observed,
                            detail=f"finding {finding.finding_id}: any_node_schedulable={finding.any_node_schedulable}",
                            detected_at=time.monotonic(),
                            diagnosed_at=time.monotonic(),
                        )
                    return _CheckResult(
                        outcome=DetectionOutcome.WRONG_CATEGORY,
                        observed=observed,
                        detail=f"finding {finding.finding_id} says '{observed}', expected '{spec.expected}'",
                        detected_at=time.monotonic(),
                    )
            time.sleep(_POLL_INTERVAL_SECONDS)
        return _CheckResult(
            outcome=DetectionOutcome.TIMED_OUT,
            observed=None,
            detail=f"no analyzable Pod for app='{spec.pod_label_app}' within {spec.timeout_seconds}s",
            detected_at=None,
        )

    def _check_network(self, spec: ScenarioSpec, applied_at_mono: float) -> _CheckResult:
        service = NetworkService(self._clients, self._network_repo, self._settings, incident_repo=self._incident_repo)
        deadline = applied_at_mono + spec.timeout_seconds
        while time.monotonic() < deadline:
            pod_name = _resolve_pod_name(self._clients, spec.namespace, spec.pod_label_app or "")
            if pod_name is not None:
                try:
                    finding = service.analyze_pod(spec.namespace, pod_name)
                except NetworkPodNotFoundError:
                    finding = None
                if finding is not None:
                    observed = finding.classification.value
                    if observed == spec.expected:
                        return _CheckResult(
                            outcome=DetectionOutcome.DETECTED,
                            observed=observed,
                            detail=f"finding {finding.finding_id}: classification={observed}",
                            detected_at=time.monotonic(),
                            diagnosed_at=time.monotonic(),
                        )
                    return _CheckResult(
                        outcome=DetectionOutcome.WRONG_CATEGORY,
                        observed=observed,
                        detail=f"finding {finding.finding_id} classified '{observed}', expected '{spec.expected}'",
                        detected_at=time.monotonic(),
                    )
            time.sleep(_POLL_INTERVAL_SECONDS)
        return _CheckResult(
            outcome=DetectionOutcome.TIMED_OUT,
            observed=None,
            detail=f"no analyzable Pod for app='{spec.pod_label_app}' within {spec.timeout_seconds}s",
            detected_at=None,
        )

    # -- SECURITY: pull-based, whole-namespace audit() --

    def _check_security(self, spec: ScenarioSpec, applied_at_mono: float) -> _CheckResult:
        service = SecurityService(self._clients, self._security_repo, self._settings)
        deadline = applied_at_mono + spec.timeout_seconds
        while time.monotonic() < deadline:
            service.audit(namespaces=[spec.namespace])
            for finding in self._security_repo.list_findings(namespace=spec.namespace):
                if finding.workload_name != spec.resource_name_prefix:
                    continue
                if finding.rule_id == spec.expected:
                    return _CheckResult(
                        outcome=DetectionOutcome.DETECTED,
                        observed=finding.rule_id,
                        detail=f"finding {finding.finding_id}: rule_id={finding.rule_id}",
                        detected_at=time.monotonic(),
                        diagnosed_at=time.monotonic(),
                    )
            time.sleep(_POLL_INTERVAL_SECONDS)
        existing = [f for f in self._security_repo.list_findings(namespace=spec.namespace) if f.workload_name == spec.resource_name_prefix]
        if existing:
            return _CheckResult(
                outcome=DetectionOutcome.WRONG_CATEGORY,
                observed=existing[0].rule_id,
                detail=f"finding(s) exist ({[f.rule_id for f in existing]}) but none matched expected '{spec.expected}'",
                detected_at=None,
            )
        return _CheckResult(
            outcome=DetectionOutcome.TIMED_OUT,
            observed=None,
            detail=f"no finding for workload '{spec.resource_name_prefix}' within {spec.timeout_seconds}s",
            detected_at=None,
        )

    # -- false-positive rate: a whole-battery property, see §5 --

    def _count_false_positives(self) -> tuple[int, int]:
        known_prefixes = [s.resource_name_prefix for s in SCENARIOS]

        def _is_known(name: str | None) -> bool:
            return name is not None and any(name.startswith(p) for p in known_prefixes)

        total = 0
        unattributed = 0

        for incident in self._incident_repo.list(namespace=None):
            if incident.status.value in ("RESOLVED", "SUPERSEDED"):
                continue
            total += 1
            if not _is_known(incident.resource.name):
                unattributed += 1

        for rec in self._optimization_repo.list_recommendations():
            total += 1
            if not _is_known(rec.workload_name):
                unattributed += 1

        for scheduling_finding in self._scheduling_repo.list_findings():
            total += 1
            if not _is_known(scheduling_finding.workload_name) and not _is_known(scheduling_finding.pod_name):
                unattributed += 1

        for network_finding in self._network_repo.list_findings():
            total += 1
            if not _is_known(network_finding.workload_name) and not _is_known(network_finding.target_pod_name):
                unattributed += 1

        for security_finding in self._security_repo.list_findings():
            total += 1
            if not _is_known(security_finding.workload_name):
                unattributed += 1

        return unattributed, total


def build_evaluation_repository(settings: Settings) -> EvaluationRepository:
    from kubesentinel.persistence.evaluation_repo import SQLiteEvaluationRepository

    return SQLiteEvaluationRepository(settings.db_path)

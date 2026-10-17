"""Orchestrates node inventory -> analyzer -> persistence for the
scheduling advisor. The one entry point the API/CLI call through, and the
only module in this package with side effects (cluster reads +
persistence) -- mirrors `optimization/service.py::OptimizationService`'s
own role exactly.
"""

from __future__ import annotations

import logging

from kubernetes.client.rest import ApiException

from kubesentinel.metrics.registry import scheduling_analysis_total
from kubesentinel.models.scheduling import SchedulingFinding
from kubesentinel.observer.client import ClusterClients
from kubesentinel.optimization.ownership import resolve_pod_owner
from kubesentinel.persistence.interface import IncidentRepository, SchedulingRepository
from kubesentinel.scheduling.analyzer import analyze_pod_scheduling, deserialize_pod_spec
from kubesentinel.scheduling.node_inventory import build_node_inventory, build_pod_label_inventory

logger = logging.getLogger(__name__)


class PodNotFoundError(Exception):
    pass


class FindingNotFoundError(Exception):
    pass


class InvalidPodSpecError(Exception):
    """Raised when a hypothetical Pod spec can't be deserialized -- a
    client input error (422), distinct from the cluster being unreachable
    (503), which `analyze_hypothetical` can also raise further down the
    same call."""


class SchedulingService:
    def __init__(
        self,
        clients: ClusterClients,
        repo: SchedulingRepository,
        incident_repo: IncidentRepository | None = None,
    ):
        self._clients = clients
        self._repo = repo
        self._incident_repo = incident_repo

    def _open_incident_ids_for(self, *uids: str) -> list[str]:
        """Same dual-UID, deduped lookup `OptimizationService` already uses
        for its own `ACTIVE_INCIDENT` flag -- an incident can be opened
        against a Pod's own UID or its owning workload's UID, depending on
        which diagnostic rule found it first. Returns `[]`, never `None`,
        when no incident repository is wired up."""
        if self._incident_repo is None:
            return []
        seen: dict[str, None] = dict.fromkeys(uid for uid in uids if uid)
        incident_ids: list[str] = []
        for uid in seen:
            incident = self._incident_repo.find_open_by_resource_uid(uid)
            if incident is not None and incident.incident_id not in incident_ids:
                incident_ids.append(incident.incident_id)
        return incident_ids

    def analyze_pod(self, namespace: str, pod_name: str) -> SchedulingFinding:
        """Analyzes a real, already-existing Pod -- the common case:
        explaining why an already-Pending Pod isn't scheduled. Never checks
        namespace ResourceQuota (see `analyzer.py`'s own docstring for why
        that check is meaningless for a Pod that already exists)."""
        try:
            try:
                pod = self._clients.core.read_namespaced_pod(name=pod_name, namespace=namespace)
            except ApiException as exc:
                if exc.status == 404:
                    raise PodNotFoundError(f"no Pod {namespace}/{pod_name}") from exc
                raise

            nodes = build_node_inventory(self._clients)
            pods = build_pod_label_inventory(self._clients)
            replicasets = self._clients.apps.list_namespaced_replica_set(namespace=namespace).items
            replicasets_by_uid = {rs.metadata.uid: rs for rs in replicasets}
            owner = resolve_pod_owner(pod, replicasets_by_uid)

            scheduler_message = None
            for condition in pod.status.conditions or []:
                if condition.type == "PodScheduled" and condition.status == "False":
                    scheduler_message = condition.message

            finding = analyze_pod_scheduling(
                pod.spec,
                nodes,
                namespace=namespace,
                pods=pods,
                pod_uid=pod.metadata.uid,
                check_quota=False,
                pod_name=pod_name,
                workload_kind=owner.kind.value if owner else None,
                workload_name=owner.name if owner else None,
                scheduler_message=scheduler_message,
            )
            finding.related_incident_ids = self._open_incident_ids_for(pod.metadata.uid, owner.uid if owner else "")
        except Exception:  # noqa: BLE001 - PodNotFoundError included; any failure here means "failed", not a finding
            scheduling_analysis_total.labels(result="failed").inc()
            raise

        self._repo.save_finding(finding)
        scheduling_analysis_total.labels(result="schedulable" if finding.any_node_schedulable else "unschedulable").inc()
        return finding

    def analyze_hypothetical(self, namespace: str, raw_pod_spec: dict) -> SchedulingFinding:
        """Simulates whether a not-yet-created Pod spec would be admitted
        (namespace ResourceQuota) and schedulable -- the only path where
        the quota check is meaningful."""
        try:
            pod_spec = deserialize_pod_spec(raw_pod_spec)
        except Exception as exc:  # noqa: BLE001 - the deserializer raises various types for malformed input
            raise InvalidPodSpecError(f"invalid pod_spec: {exc}") from exc

        try:
            nodes = build_node_inventory(self._clients)
            pods = build_pod_label_inventory(self._clients)
            try:
                quotas = self._clients.core.list_namespaced_resource_quota(namespace=namespace).items
            except ApiException as exc:
                logger.info("could not list ResourceQuota in %s, skipping quota check: %s", namespace, exc)
                quotas = []

            finding = analyze_pod_scheduling(
                pod_spec,
                nodes,
                namespace=namespace,
                pods=pods,
                pod_uid=None,
                quotas=quotas,
                check_quota=True,
            )
        except Exception:  # noqa: BLE001 - any failure here means "failed", not a finding
            scheduling_analysis_total.labels(result="failed").inc()
            raise

        self._repo.save_finding(finding)
        scheduling_analysis_total.labels(result="schedulable" if finding.any_node_schedulable else "unschedulable").inc()
        return finding

    def get_finding(self, finding_id: str) -> SchedulingFinding:
        finding = self._repo.get_finding(finding_id)
        if finding is None:
            raise FindingNotFoundError(f"no scheduling finding {finding_id}")
        return finding

    def list_findings(
        self, *, namespace: str | None = None, schedulable: bool | None = None
    ) -> list[SchedulingFinding]:
        return self._repo.list_findings(namespace=namespace, schedulable=schedulable)

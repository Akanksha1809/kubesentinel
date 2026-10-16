"""Orchestrates Service/policy/DNS inventory -> analyzer -> persistence for
the network troubleshooting engine. The one entry point the API/CLI call
through, and the only module in this package with side effects (cluster
reads + persistence) -- mirrors `scheduling/service.py::SchedulingService`'s
own shape exactly, which itself mirrors `optimization/service.py`.
"""

from __future__ import annotations

from kubernetes.client.rest import ApiException

from kubesentinel.metrics.registry import network_analysis_total
from kubesentinel.models.network import NetworkFinding
from kubesentinel.network.analyzer import analyze_network
from kubesentinel.network.dns_health import check_dns_health
from kubesentinel.network.policy_inventory import build_applicable_policies
from kubesentinel.network.service_inventory import build_service_match_results
from kubesentinel.observer.client import ClusterClients
from kubesentinel.optimization.ownership import resolve_pod_owner
from kubesentinel.persistence.interface import IncidentRepository, NetworkRepository


class PodNotFoundError(Exception):
    pass


class FindingNotFoundError(Exception):
    pass


class NetworkService:
    def __init__(
        self,
        clients: ClusterClients,
        repo: NetworkRepository,
        incident_repo: IncidentRepository | None = None,
    ):
        self._clients = clients
        self._repo = repo
        self._incident_repo = incident_repo

    def _open_incident_ids_for(self, *uids: str) -> list[str]:
        """Same dual-UID, deduped lookup `OptimizationService`/
        `SchedulingService` already use."""
        if self._incident_repo is None:
            return []
        seen: dict[str, None] = dict.fromkeys(uid for uid in uids if uid)
        incident_ids: list[str] = []
        for uid in seen:
            incident = self._incident_repo.find_open_by_resource_uid(uid)
            if incident is not None and incident.incident_id not in incident_ids:
                incident_ids.append(incident.incident_id)
        return incident_ids

    def analyze_pod(self, namespace: str, pod_name: str) -> NetworkFinding:
        """Explains the given Pod's inbound Service-based connectivity
        story: which Services select it, whether its ports would resolve,
        which NetworkPolicies apply, and whether cluster DNS looks
        healthy."""
        try:
            try:
                pod = self._clients.core.read_namespaced_pod(name=pod_name, namespace=namespace)
            except ApiException as exc:
                if exc.status == 404:
                    raise PodNotFoundError(f"no Pod {namespace}/{pod_name}") from exc
                raise

            replicasets = self._clients.apps.list_namespaced_replica_set(namespace=namespace).items
            replicasets_by_uid = {rs.metadata.uid: rs for rs in replicasets}
            owner = resolve_pod_owner(pod, replicasets_by_uid)

            dns_health = check_dns_health(self._clients)
            service_results = build_service_match_results(self._clients, namespace)
            applicable_policies = build_applicable_policies(self._clients, namespace, pod)

            finding = analyze_network(
                namespace=namespace,
                target_pod_name=pod_name,
                dns_health=dns_health,
                service_results=service_results,
                applicable_policies=applicable_policies,
                workload_kind=owner.kind.value if owner else None,
                workload_name=owner.name if owner else None,
            )
            finding.related_incident_ids = self._open_incident_ids_for(pod.metadata.uid, owner.uid if owner else "")
        except Exception:  # noqa: BLE001 - PodNotFoundError included; any failure here means "failed", not a finding
            network_analysis_total.labels(result="failed").inc()
            raise

        self._repo.save_finding(finding)
        network_analysis_total.labels(result=finding.classification.value.lower()).inc()
        return finding

    def get_finding(self, finding_id: str) -> NetworkFinding:
        finding = self._repo.get_finding(finding_id)
        if finding is None:
            raise FindingNotFoundError(f"no network finding {finding_id}")
        return finding

    def list_findings(
        self, *, namespace: str | None = None, classification: str | None = None
    ) -> list[NetworkFinding]:
        return self._repo.list_findings(namespace=namespace, classification=classification)

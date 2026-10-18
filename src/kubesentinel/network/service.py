"""Orchestrates Service/policy/DNS inventory -> analyzer -> persistence for
the network troubleshooting engine. The one entry point the API/CLI call
through, and the only module in this package with side effects (cluster
reads + persistence) -- mirrors `scheduling/service.py::SchedulingService`'s
own shape exactly, which itself mirrors `optimization/service.py`.

`run_probe` (docs/NETWORK_DIAGNOSTICS.md Section 12) is the exception to
this module's otherwise entirely-read-only history: the gating logic
(enabled check, namespace allowlist, cooldown, concurrency) lives here,
right next to `analyze_pod`, so a reviewer checking "does this module
ever mutate the cluster" finds the one path that does and every check
guarding it in the same file, rather than split across modules.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kubernetes.client.rest import ApiException

from kubesentinel.config import Settings
from kubesentinel.metrics.registry import network_analysis_total, network_probe_total
from kubesentinel.models.network import ConnectivityProbeResult, NetworkFinding
from kubesentinel.network import probe as probe_module
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


class ProbeDisabledError(Exception):
    """`network_probe_enabled` is False -- the whole feature is off."""


class ProbeDeniedError(Exception):
    """The probe is enabled, but this specific request is refused: the
    namespace isn't on the allowlist, a cooldown is active, or the
    per-namespace concurrency limit is already reached."""


class ProbeTargetError(Exception):
    """The target Service doesn't exist, or the requested port doesn't
    resolve against any of its real ports."""


class NetworkService:
    def __init__(
        self,
        clients: ClusterClients,
        repo: NetworkRepository,
        settings: Settings,
        incident_repo: IncidentRepository | None = None,
    ):
        self._clients = clients
        self._repo = repo
        self._settings = settings
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

    def run_probe(
        self,
        namespace: str,
        service_name: str,
        port: int | str,
        *,
        related_finding_id: str | None = None,
    ) -> ConnectivityProbeResult:
        """Creates, waits on, reads the result of, and deletes one
        short-lived diagnostic Job testing DNS resolution and a raw TCP
        connect against a real Service -- see docs/NETWORK_DIAGNOSTICS.md
        Section 12 for the full design and every safety property this
        method and `network/probe.py` together enforce. Never raises past
        the gating checks without attempting cleanup: once a Job exists,
        deletion is attempted regardless of how the probe itself turns
        out."""
        if not self._settings.network_probe_enabled:
            network_probe_total.labels(result="denied").inc()
            raise ProbeDisabledError("active connectivity probing is disabled (network_probe_enabled=False)")

        allowed = self._settings.network_probe_allowed_namespace_list
        if namespace not in allowed:
            network_probe_total.labels(result="denied").inc()
            raise ProbeDeniedError(f"namespace '{namespace}' is not in the probe namespace allowlist {allowed}")

        api_timeout = self._settings.network_probe_api_timeout_seconds
        try:
            service = self._clients.core.read_namespaced_service(
                name=service_name, namespace=namespace, _request_timeout=api_timeout
            )
        except ApiException as exc:
            network_probe_total.labels(result="denied").inc()
            if exc.status == 404:
                raise ProbeTargetError(f"no Service {namespace}/{service_name}") from exc
            raise

        resolved_port = probe_module.resolve_service_port(service, port)
        if resolved_port is None:
            network_probe_total.labels(result="denied").inc()
            raise ProbeTargetError(f"port {port!r} does not resolve against any of {namespace}/{service_name}'s ports")

        recent = self._repo.list_probe_results(namespace=namespace, service_name=service_name, port=resolved_port)
        if recent:
            elapsed = (datetime.now(timezone.utc) - recent[0].requested_at).total_seconds()
            if elapsed < self._settings.network_probe_cooldown_seconds:
                network_probe_total.labels(result="denied").inc()
                raise ProbeDeniedError(
                    f"cooldown active: this target was last probed {elapsed:.0f}s ago, cooldown is "
                    f"{self._settings.network_probe_cooldown_seconds}s"
                )

        active = probe_module.count_active_probe_jobs(self._clients, namespace, api_timeout_seconds=api_timeout)
        if active >= self._settings.network_probe_max_concurrent_per_namespace:
            network_probe_total.labels(result="denied").inc()
            raise ProbeDeniedError(
                f"{active} probe(s) already active in namespace '{namespace}', at or above the limit of "
                f"{self._settings.network_probe_max_concurrent_per_namespace}"
            )

        cluster_ip = service.spec.cluster_ip
        tcp_applicable = cluster_ip not in (None, "", "None")
        requested_at = datetime.now(timezone.utc)
        job_name = probe_module.new_probe_job_name()
        # The Service's full in-namespace FQDN, never the bare short name
        # -- see Settings.network_probe_cluster_domain's own docstring for
        # why (a live-caught BusyBox nslookup exit-code quirk).
        fqdn = f"{service_name}.{namespace}.svc.{self._settings.network_probe_cluster_domain}"
        job = probe_module.build_probe_job(
            job_name=job_name,
            host=fqdn,
            cluster_ip=cluster_ip if tcp_applicable else None,
            port=resolved_port,
            timeout_seconds=self._settings.network_probe_tcp_timeout_seconds,
            active_deadline_seconds=self._settings.network_probe_job_timeout_seconds,
            ttl_seconds=self._settings.network_probe_job_ttl_seconds,
        )

        error: str | None = None
        log = ""
        try:
            probe_module.create_probe_job(self._clients, namespace, job, api_timeout_seconds=api_timeout)
            completed = probe_module.wait_for_job_completion(
                self._clients,
                namespace,
                job_name,
                poll_timeout_seconds=self._settings.network_probe_poll_timeout_seconds,
                poll_interval_seconds=self._settings.network_probe_poll_interval_seconds,
                api_timeout_seconds=api_timeout,
            )
            if not completed:
                error = "timed out waiting for the probe Job to complete"
            log = probe_module.read_job_pod_log(self._clients, namespace, job_name, api_timeout_seconds=api_timeout)
        finally:
            probe_module.delete_probe_job(self._clients, namespace, job_name, api_timeout_seconds=api_timeout)

        dns_resolved, tcp_connected = probe_module.parse_probe_log(log, tcp_applicable=tcp_applicable)
        result = probe_module.build_result(
            probe_id=job_name,
            namespace=namespace,
            service_name=service_name,
            port=resolved_port,
            dns_resolved=dns_resolved,
            tcp_connected=tcp_connected,
            tcp_applicable=tcp_applicable,
            raw_log=log,
            error=error,
            related_finding_id=related_finding_id,
            requested_at=requested_at,
        )
        self._repo.save_probe_result(result)

        if error:
            network_probe_total.labels(result="timeout").inc()
        elif dns_resolved is False:
            network_probe_total.labels(result="dns_failed").inc()
        elif tcp_applicable and tcp_connected is False:
            network_probe_total.labels(result="dns_ok_tcp_failed").inc()
        else:
            network_probe_total.labels(result="dns_ok_tcp_ok").inc()
        return result

    def get_probe_result(self, probe_id: str) -> ConnectivityProbeResult:
        result = self._repo.get_probe_result(probe_id)
        if result is None:
            raise FindingNotFoundError(f"no probe result {probe_id}")
        return result

    def list_probe_results(
        self, *, namespace: str | None = None, service_name: str | None = None, port: int | None = None
    ) -> list[ConnectivityProbeResult]:
        return self._repo.list_probe_results(namespace=namespace, service_name=service_name, port=port)

"""The active connectivity probe (docs/NETWORK_DIAGNOSTICS.md Section 12)
-- this project's first new mutation capability outside the fully
policy-gated `RemediationExecutor`. Creates one short-lived, auto-cleaned-
up `batch/v1` Job per probe request, running a fixed script that tests
DNS resolution and a raw TCP connect against a target Service, then reads
its result from the Job's own Pod logs and deletes it.

Every safety property named in the design doc lives here or in
`NetworkService.run_probe` (the orchestration entry point that calls
this module): the off-by-default gate, the namespace allowlist, the
cooldown/concurrency limits, and -- the one enforced entirely by this
module's own construction, not by a runtime check -- the fact that the
container's command is always the exact same fixed string, with every
piece of caller-supplied data flowing in only through typed `V1EnvVar`
entries, never through string formatting into the script itself.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from kubesentinel.models.network import ConnectivityProbeResult
from kubesentinel.observer.client import ClusterClients

#: Identifies every Job/Pod this module creates -- used both to find a
#: probe's own Pod for log-reading and to count in-flight probes for the
#: concurrency gate (NetworkService.run_probe).
PROBE_LABEL_KEY = "kubesentinel.io/probe"
PROBE_LABEL_VALUE = "true"

_PROBE_IMAGE = "busybox:1.36"  # pinned, never :latest -- see Section 12.2

#: Fixed, hardcoded script -- never built from caller input. Every piece
#: of caller-supplied data (host, cluster IP, port, timeout) reaches the
#: container exclusively through the env vars referenced here, which the
#: shell treats as data, not as script text. `set -e` stops after a
#: failing DNS lookup rather than attempting the TCP check with data this
#: project's own read_namespaced_service call already resolved
#: independently of the probe's own DNS -- see Section 12.3 for why that
#: doesn't undermine the TCP check's independence from DNS.
_PROBE_SCRIPT = """set -e
echo KUBESENTINEL_PROBE_DNS_ATTEMPT
nslookup "$KUBESENTINEL_PROBE_HOST" && echo KUBESENTINEL_PROBE_DNS_OK
if [ -n "$KUBESENTINEL_PROBE_CLUSTER_IP" ]; then
  echo KUBESENTINEL_PROBE_TCP_ATTEMPT
  nc -z -w "$KUBESENTINEL_PROBE_TIMEOUT" "$KUBESENTINEL_PROBE_CLUSTER_IP" "$KUBESENTINEL_PROBE_PORT" && echo KUBESENTINEL_PROBE_TCP_OK
fi
"""


def new_probe_job_name() -> str:
    return f"kubesentinel-probe-{uuid.uuid4().hex[:12]}"


def resolve_service_port(service: k8s_client.V1Service, port: int | str) -> int | None:
    """Resolves a caller-supplied port (a number, or a Service port's own
    `name`) against `service.spec.ports` -- returns the real port number,
    or None if nothing matches. A numeric `port` that isn't actually one
    of the Service's own ports is also treated as unresolved, rather than
    probed anyway -- this project's own `read_namespaced_service` already
    knows every real port, so there's no reason to trust an unverified
    caller-supplied number over it."""
    for svc_port in service.spec.ports or []:
        if isinstance(port, str):
            if svc_port.name == port:
                return svc_port.port
        elif svc_port.port == port:
            return svc_port.port
    return None


def build_probe_job(
    *,
    job_name: str,
    host: str,
    cluster_ip: str | None,
    port: int,
    timeout_seconds: int,
    active_deadline_seconds: int,
    ttl_seconds: int,
) -> k8s_client.V1Job:
    """`cluster_ip=None` (a headless Service) means the script's own `[ -n
    ... ]` guard skips the TCP section entirely -- DNS-only, consistent
    with Section 12.1's documented headless-Service handling."""
    container = k8s_client.V1Container(
        name="probe",
        image=_PROBE_IMAGE,
        command=["sh", "-c", _PROBE_SCRIPT],
        env=[
            k8s_client.V1EnvVar(name="KUBESENTINEL_PROBE_HOST", value=host),
            k8s_client.V1EnvVar(name="KUBESENTINEL_PROBE_CLUSTER_IP", value=cluster_ip or ""),
            k8s_client.V1EnvVar(name="KUBESENTINEL_PROBE_PORT", value=str(port)),
            k8s_client.V1EnvVar(name="KUBESENTINEL_PROBE_TIMEOUT", value=str(timeout_seconds)),
        ],
        security_context=k8s_client.V1SecurityContext(
            allow_privilege_escalation=False,
            privileged=False,
            run_as_non_root=True,
            # busybox:1.36 defaults to root (uid 0) -- runAsNonRoot alone,
            # without an explicit non-root runAsUser, makes the kubelet
            # refuse to start the container at all ("container has
            # runAsNonRoot and image will run as root"), caught live: the
            # probe Job never got a Pod past CreateContainerConfigError,
            # which looked identical to a hung Job from the outside until
            # `kubectl describe pod` showed the real reason. 65534 is the
            # conventional "nobody" UID; nothing this script runs
            # (sh/nslookup/nc) needs a real /etc/passwd entry for it.
            run_as_user=65534,
            read_only_root_filesystem=True,
        ),
    )
    pod_spec = k8s_client.V1PodSpec(
        restart_policy="Never",
        automount_service_account_token=False,
        containers=[container],
    )
    return k8s_client.V1Job(
        metadata=k8s_client.V1ObjectMeta(name=job_name, labels={PROBE_LABEL_KEY: PROBE_LABEL_VALUE}),
        spec=k8s_client.V1JobSpec(
            active_deadline_seconds=active_deadline_seconds,
            backoff_limit=0,
            ttl_seconds_after_finished=ttl_seconds,
            template=k8s_client.V1PodTemplateSpec(
                metadata=k8s_client.V1ObjectMeta(labels={PROBE_LABEL_KEY: PROBE_LABEL_VALUE}),
                spec=pod_spec,
            ),
        ),
    )


def parse_probe_log(log: str, *, tcp_applicable: bool) -> tuple[bool | None, bool | None]:
    """Classifies a probe result from exactly which
    `KUBESENTINEL_PROBE_*_OK` markers are present -- never by parsing
    `nslookup`/`nc`'s own free-text output, which differs across BusyBox
    versions. Returns `(dns_resolved, tcp_connected)`; either is `None`
    when its own `_ATTEMPT` marker never appeared at all (the Job never
    ran that far -- e.g. it was killed before starting)."""
    dns_resolved = "KUBESENTINEL_PROBE_DNS_OK" in log if "KUBESENTINEL_PROBE_DNS_ATTEMPT" in log else None
    if not tcp_applicable:
        return dns_resolved, None
    tcp_connected = "KUBESENTINEL_PROBE_TCP_OK" in log if "KUBESENTINEL_PROBE_TCP_ATTEMPT" in log else None
    return dns_resolved, tcp_connected


def count_active_probe_jobs(clients: ClusterClients, namespace: str, *, api_timeout_seconds: float) -> int:
    """Jobs carrying this module's own label that haven't reached a
    terminal state yet -- the concurrency gate's own evidence."""
    jobs = clients.batch.list_namespaced_job(
        namespace=namespace,
        label_selector=f"{PROBE_LABEL_KEY}={PROBE_LABEL_VALUE}",
        _request_timeout=api_timeout_seconds,
    ).items
    active = 0
    for job in jobs:
        status = job.status
        if status and ((status.succeeded or 0) > 0 or (status.failed or 0) > 0):
            continue
        active += 1
    return active


def create_probe_job(
    clients: ClusterClients, namespace: str, job: k8s_client.V1Job, *, api_timeout_seconds: float
) -> None:
    clients.batch.create_namespaced_job(namespace=namespace, body=job, _request_timeout=api_timeout_seconds)


def wait_for_job_completion(
    clients: ClusterClients,
    namespace: str,
    job_name: str,
    *,
    poll_timeout_seconds: float,
    poll_interval_seconds: float,
    api_timeout_seconds: float,
) -> bool:
    """Polls the Job's own status until it reports success or failure, or
    `poll_timeout_seconds` elapses. Returns True if the Job reached a
    terminal state in time, False on timeout -- never raises on timeout,
    the caller decides what an inconclusive result means.

    `api_timeout_seconds` bounds each individual `read_namespaced_job_status`
    call -- without it, a single slow/hung API server response could block
    far longer than `poll_timeout_seconds` itself, since that deadline is
    only checked *between* calls, never during one. Caught live: an
    unbounded call left a probe hanging for over 20 minutes against a
    genuinely overloaded apiserver, nowhere near the ~20s this function's
    own docstring promised."""
    deadline = time.monotonic() + poll_timeout_seconds
    while time.monotonic() < deadline:
        try:
            job = clients.batch.read_namespaced_job_status(
                name=job_name, namespace=namespace, _request_timeout=api_timeout_seconds
            )
        except ApiException:
            return False
        status = job.status
        if status and ((status.succeeded or 0) > 0 or (status.failed or 0) > 0):
            return True
        time.sleep(poll_interval_seconds)
    return False


def read_job_pod_log(clients: ClusterClients, namespace: str, job_name: str, *, api_timeout_seconds: float) -> str:
    """Reads the log of the one Pod this Job created. Returns "" if no Pod
    or no log is available yet -- a missing log is not an error this
    function raises, just an empty result the caller classifies as
    "nothing observed." """
    try:
        pods = clients.core.list_namespaced_pod(
            namespace=namespace, label_selector=f"job-name={job_name}", _request_timeout=api_timeout_seconds
        ).items
    except ApiException:
        return ""
    if not pods:
        return ""
    try:
        return clients.core.read_namespaced_pod_log(
            name=pods[0].metadata.name, namespace=namespace, _request_timeout=api_timeout_seconds
        )
    except ApiException:
        return ""


def delete_probe_job(clients: ClusterClients, namespace: str, job_name: str, *, api_timeout_seconds: float) -> None:
    """Best-effort: a failure here is backstopped by the Job's own
    `ttlSecondsAfterFinished` (Section 12.2 point 6), so this never raises
    -- the caller's own cleanup step should never itself fail the probe
    just because deletion hit a transient error."""
    try:
        clients.batch.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            propagation_policy="Background",
            _request_timeout=api_timeout_seconds,
        )
    except ApiException:
        pass


def build_result(
    *,
    probe_id: str,
    namespace: str,
    service_name: str,
    port: int,
    dns_resolved: bool | None,
    tcp_connected: bool | None,
    tcp_applicable: bool,
    raw_log: str,
    error: str | None,
    related_finding_id: str | None,
    requested_at: datetime,
) -> ConnectivityProbeResult:
    succeeded = bool(dns_resolved) and (tcp_connected is True if tcp_applicable else True)
    return ConnectivityProbeResult(
        probe_id=probe_id,
        namespace=namespace,
        service_name=service_name,
        port=port,
        dns_resolved=dns_resolved,
        tcp_connected=tcp_connected,
        tcp_applicable=tcp_applicable,
        succeeded=succeeded,
        raw_log=raw_log,
        error=error,
        related_finding_id=related_finding_id,
        requested_at=requested_at,
        completed_at=datetime.now(timezone.utc),
    )

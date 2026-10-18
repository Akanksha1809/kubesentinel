from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client import V1ServicePort

from kubesentinel.network import probe
from tests.unit.factories import make_service


def _items(objs):
    return SimpleNamespace(items=objs)


class TestResolveServicePort:
    def test_resolves_by_number(self):
        svc = make_service(ports=[V1ServicePort(port=80, target_port=8080)])
        assert probe.resolve_service_port(svc, 80) == 80

    def test_resolves_by_name(self):
        svc = make_service(ports=[V1ServicePort(port=80, target_port=8080, name="http")])
        assert probe.resolve_service_port(svc, "http") == 80

    def test_unresolved_number_returns_none(self):
        svc = make_service(ports=[V1ServicePort(port=80, target_port=8080)])
        assert probe.resolve_service_port(svc, 9999) is None

    def test_unresolved_name_returns_none(self):
        svc = make_service(ports=[V1ServicePort(port=80, target_port=8080, name="http")])
        assert probe.resolve_service_port(svc, "grpc") is None

    def test_no_ports_returns_none(self):
        svc = make_service(ports=[])
        assert probe.resolve_service_port(svc, 80) is None


class TestBuildProbeJob:
    def test_command_is_fixed_regardless_of_adversarial_input(self):
        """The whole safety argument rests on this: no matter what a
        caller passes as host/port, the container's own command string
        never changes -- every value flows in through env only."""
        adversarial_host = "svc$(rm -rf /); echo pwned"
        job_a = probe.build_probe_job(
            job_name="j1",
            host="normal-service",
            cluster_ip="10.0.0.1",
            port=80,
            timeout_seconds=5,
            active_deadline_seconds=15,
            ttl_seconds=60,
        )
        job_b = probe.build_probe_job(
            job_name="j1",
            host=adversarial_host,
            cluster_ip="10.0.0.1`touch /tmp/pwned`",
            port=80,
            timeout_seconds=5,
            active_deadline_seconds=15,
            ttl_seconds=60,
        )
        command_a = job_a.spec.template.spec.containers[0].command
        command_b = job_b.spec.template.spec.containers[0].command
        assert command_a == command_b
        assert command_a == ["sh", "-c", probe._PROBE_SCRIPT]

    def test_adversarial_data_only_ever_appears_in_env_values(self):
        adversarial_host = "svc$(rm -rf /)"
        job = probe.build_probe_job(
            job_name="j1",
            host=adversarial_host,
            cluster_ip="10.0.0.1",
            port=80,
            timeout_seconds=5,
            active_deadline_seconds=15,
            ttl_seconds=60,
        )
        container = job.spec.template.spec.containers[0]
        assert adversarial_host not in "".join(container.command)
        env_by_name = {e.name: e.value for e in container.env}
        assert env_by_name["KUBESENTINEL_PROBE_HOST"] == adversarial_host

    def test_headless_service_passes_empty_cluster_ip(self):
        job = probe.build_probe_job(
            job_name="j1", host="svc", cluster_ip=None, port=80, timeout_seconds=5, active_deadline_seconds=15, ttl_seconds=60
        )
        env_by_name = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
        assert env_by_name["KUBESENTINEL_PROBE_CLUSTER_IP"] == ""

    def test_job_has_bounded_active_deadline_and_ttl(self):
        job = probe.build_probe_job(
            job_name="j1", host="svc", cluster_ip="10.0.0.1", port=80, timeout_seconds=5, active_deadline_seconds=15, ttl_seconds=60
        )
        assert job.spec.active_deadline_seconds == 15
        assert job.spec.ttl_seconds_after_finished == 60
        assert job.spec.backoff_limit == 0

    def test_pod_template_passes_container_security_checks(self):
        """Mirrors what this project's own security auditor would check --
        the probe's own Pod must never trip PRIVILEGED_CONTAINER,
        ALLOW_PRIVILEGE_ESCALATION, RUN_AS_ROOT, MISSING_READ_ONLY_ROOT_FS,
        HOST_NAMESPACE_SHARING, or MUTABLE_IMAGE_TAG."""
        job = probe.build_probe_job(
            job_name="j1", host="svc", cluster_ip="10.0.0.1", port=80, timeout_seconds=5, active_deadline_seconds=15, ttl_seconds=60
        )
        pod_spec = job.spec.template.spec
        container = pod_spec.containers[0]
        assert container.security_context.privileged is False
        assert container.security_context.allow_privilege_escalation is False
        assert container.security_context.run_as_non_root is True
        # A real, live-caught bug: runAsNonRoot alone, without an
        # explicit non-root runAsUser, makes the kubelet refuse to start
        # busybox:1.36 at all (it defaults to root) -- see probe.py's own
        # comment. Pin this so it can never regress silently.
        assert container.security_context.run_as_user is not None
        assert container.security_context.run_as_user != 0
        assert container.security_context.read_only_root_filesystem is True
        assert pod_spec.automount_service_account_token is False
        assert pod_spec.restart_policy == "Never"
        assert ":" in container.image and not container.image.endswith(":latest")
        assert pod_spec.host_network is None
        assert pod_spec.host_pid is None
        assert pod_spec.host_ipc is None

    def test_job_and_pod_carry_the_probe_label(self):
        job = probe.build_probe_job(
            job_name="j1", host="svc", cluster_ip="10.0.0.1", port=80, timeout_seconds=5, active_deadline_seconds=15, ttl_seconds=60
        )
        assert job.metadata.labels[probe.PROBE_LABEL_KEY] == probe.PROBE_LABEL_VALUE
        assert job.spec.template.metadata.labels[probe.PROBE_LABEL_KEY] == probe.PROBE_LABEL_VALUE


class TestParseProbeLog:
    def test_dns_ok_tcp_ok(self):
        log = "KUBESENTINEL_PROBE_DNS_ATTEMPT\nKUBESENTINEL_PROBE_DNS_OK\nKUBESENTINEL_PROBE_TCP_ATTEMPT\nKUBESENTINEL_PROBE_TCP_OK\n"
        dns, tcp = probe.parse_probe_log(log, tcp_applicable=True)
        assert dns is True
        assert tcp is True

    def test_dns_ok_tcp_failed(self):
        log = "KUBESENTINEL_PROBE_DNS_ATTEMPT\nKUBESENTINEL_PROBE_DNS_OK\nKUBESENTINEL_PROBE_TCP_ATTEMPT\n"
        dns, tcp = probe.parse_probe_log(log, tcp_applicable=True)
        assert dns is True
        assert tcp is False

    def test_dns_failed_short_circuits_before_tcp_attempt(self):
        log = "KUBESENTINEL_PROBE_DNS_ATTEMPT\n"
        dns, tcp = probe.parse_probe_log(log, tcp_applicable=True)
        assert dns is False
        assert tcp is None

    def test_no_markers_at_all_is_not_attempted(self):
        dns, tcp = probe.parse_probe_log("", tcp_applicable=True)
        assert dns is None
        assert tcp is None

    def test_tcp_not_applicable_never_reports_tcp(self):
        log = "KUBESENTINEL_PROBE_DNS_ATTEMPT\nKUBESENTINEL_PROBE_DNS_OK\n"
        dns, tcp = probe.parse_probe_log(log, tcp_applicable=False)
        assert dns is True
        assert tcp is None


class TestCountActiveProbeJobs:
    def test_counts_only_unfinished_jobs(self):
        clients = MagicMock()
        running = SimpleNamespace(status=SimpleNamespace(succeeded=0, failed=0))
        succeeded = SimpleNamespace(status=SimpleNamespace(succeeded=1, failed=0))
        failed = SimpleNamespace(status=SimpleNamespace(succeeded=0, failed=1))
        clients.batch.list_namespaced_job.return_value = _items([running, succeeded, failed])

        assert probe.count_active_probe_jobs(clients, "kubesentinel-lab", api_timeout_seconds=10.0) == 1

    def test_no_jobs_is_zero(self):
        clients = MagicMock()
        clients.batch.list_namespaced_job.return_value = _items([])
        assert probe.count_active_probe_jobs(clients, "kubesentinel-lab", api_timeout_seconds=10.0) == 0


class TestEveryApiCallIsRequestTimeoutBounded:
    """A real bug caught live: a single slow/hung apiserver call inside
    the polling loop could block far longer than any configured
    "timeout" implied, because `_request_timeout` was never passed to
    any Kubernetes client call in this module. Pins that every one of
    them now is -- never again silently unbounded."""

    def test_count_active_probe_jobs_passes_request_timeout(self):
        clients = MagicMock()
        clients.batch.list_namespaced_job.return_value = _items([])
        probe.count_active_probe_jobs(clients, "ns", api_timeout_seconds=7.5)
        assert clients.batch.list_namespaced_job.call_args.kwargs["_request_timeout"] == 7.5

    def test_create_probe_job_passes_request_timeout(self):
        clients = MagicMock()
        job = probe.build_probe_job(
            job_name="j1", host="svc", cluster_ip="10.0.0.1", port=80, timeout_seconds=5, active_deadline_seconds=15, ttl_seconds=60
        )
        probe.create_probe_job(clients, "ns", job, api_timeout_seconds=7.5)
        assert clients.batch.create_namespaced_job.call_args.kwargs["_request_timeout"] == 7.5

    def test_wait_for_job_completion_passes_request_timeout(self):
        clients = MagicMock()
        clients.batch.read_namespaced_job_status.return_value = SimpleNamespace(
            status=SimpleNamespace(succeeded=1, failed=0)
        )
        probe.wait_for_job_completion(
            clients, "ns", "j1", poll_timeout_seconds=5, poll_interval_seconds=0.1, api_timeout_seconds=7.5
        )
        assert clients.batch.read_namespaced_job_status.call_args.kwargs["_request_timeout"] == 7.5

    def test_read_job_pod_log_passes_request_timeout_to_both_calls(self):
        clients = MagicMock()
        clients.core.list_namespaced_pod.return_value = _items([SimpleNamespace(metadata=SimpleNamespace(name="pod-1"))])
        clients.core.read_namespaced_pod_log.return_value = "log"
        probe.read_job_pod_log(clients, "ns", "j1", api_timeout_seconds=7.5)
        assert clients.core.list_namespaced_pod.call_args.kwargs["_request_timeout"] == 7.5
        assert clients.core.read_namespaced_pod_log.call_args.kwargs["_request_timeout"] == 7.5

    def test_delete_probe_job_passes_request_timeout(self):
        clients = MagicMock()
        probe.delete_probe_job(clients, "ns", "j1", api_timeout_seconds=7.5)
        assert clients.batch.delete_namespaced_job.call_args.kwargs["_request_timeout"] == 7.5


class TestBuildResult:
    def test_succeeded_true_when_dns_and_tcp_ok(self):
        result = probe.build_result(
            probe_id="p1",
            namespace="ns",
            service_name="svc",
            port=80,
            dns_resolved=True,
            tcp_connected=True,
            tcp_applicable=True,
            raw_log="log",
            error=None,
            related_finding_id=None,
            requested_at=datetime.now(timezone.utc),
        )
        assert result.succeeded is True

    def test_succeeded_false_when_tcp_failed(self):
        result = probe.build_result(
            probe_id="p1",
            namespace="ns",
            service_name="svc",
            port=80,
            dns_resolved=True,
            tcp_connected=False,
            tcp_applicable=True,
            raw_log="log",
            error=None,
            related_finding_id=None,
            requested_at=datetime.now(timezone.utc),
        )
        assert result.succeeded is False

    def test_succeeded_true_for_headless_service_when_dns_ok(self):
        result = probe.build_result(
            probe_id="p1",
            namespace="ns",
            service_name="svc",
            port=80,
            dns_resolved=True,
            tcp_connected=None,
            tcp_applicable=False,
            raw_log="log",
            error=None,
            related_finding_id=None,
            requested_at=datetime.now(timezone.utc),
        )
        assert result.succeeded is True

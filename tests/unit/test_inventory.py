from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client import (
    V1Container,
    V1ContainerState,
    V1ContainerStateTerminated,
    V1ContainerStatus,
    V1DaemonSet,
    V1DaemonSetSpec,
    V1DaemonSetStatus,
    V1Job,
    V1JobSpec,
    V1JobStatus,
    V1LabelSelector,
    V1ObjectMeta,
    V1PodCondition,
    V1PodSpec,
    V1PodTemplateSpec,
    V1ResourceRequirements,
    V1StatefulSet,
    V1StatefulSetSpec,
    V1StatefulSetStatus,
)

from kubesentinel.models.optimization import WorkloadKind
from kubesentinel.optimization.inventory import build_workload_inventory
from tests.unit.factories import make_deployment, make_pod, make_replica_set

NAMESPACE = "kubesentinel-lab"


def _items(objs):
    return SimpleNamespace(items=objs)


def _clients(pods=(), replica_sets=(), deployments=(), stateful_sets=(), daemon_sets=(), jobs=()):
    clients = MagicMock()
    clients.core.list_namespaced_pod.return_value = _items(list(pods))
    clients.apps.list_namespaced_replica_set.return_value = _items(list(replica_sets))
    clients.apps.list_namespaced_deployment.return_value = _items(list(deployments))
    clients.apps.list_namespaced_stateful_set.return_value = _items(list(stateful_sets))
    clients.apps.list_namespaced_daemon_set.return_value = _items(list(daemon_sets))
    clients.batch.list_namespaced_job.return_value = _items(list(jobs))
    return clients


def _container_with_resources(name="app", cpu_request=None, memory_request=None, cpu_limit=None, memory_limit=None):
    requests = {}
    if cpu_request:
        requests["cpu"] = cpu_request
    if memory_request:
        requests["memory"] = memory_request
    limits = {}
    if cpu_limit:
        limits["cpu"] = cpu_limit
    if memory_limit:
        limits["memory"] = memory_limit
    resources = V1ResourceRequirements(requests=requests or None, limits=limits or None)
    return V1Container(name=name, resources=resources)


def _ready_pod(name, uid, owner, restart_count=0):
    return make_pod(
        name=name,
        uid=uid,
        owner=owner,
        conditions=[V1PodCondition(type="Ready", status="True")],
        container_statuses=[V1ContainerStatus(name="app", ready=True, restart_count=restart_count, image="x", image_id="x")],
    )


class TestDeploymentInventory:
    def test_builds_one_workload_snapshot_for_multiple_replicas(self):
        # make_replica_set's owner reference always names "test-deployment"
        # (the factory's fixed Deployment-owner name) -- the actual listed
        # Deployment object below is given the same name/uid so the two stay
        # consistent, matching what a real cluster guarantees.
        deployment = make_deployment(name="test-deployment", uid="deploy-uid-1", desired_replicas=2, ready_replicas=2)
        deployment.spec.template.spec = V1PodSpec(
            containers=[_container_with_resources(cpu_request="500m", memory_request="256Mi")]
        )
        replica_set = make_replica_set(name="my-app-abc123", owner_uid="deploy-uid-1")
        pods = [
            _ready_pod("my-app-abc123-1", "pod-uid-1", owner=("ReplicaSet", "my-app-abc123", "rs-uid-1")),
            _ready_pod("my-app-abc123-2", "pod-uid-2", owner=("ReplicaSet", "my-app-abc123", "rs-uid-1")),
        ]
        clients = _clients(
            pods=pods, replica_sets=[_with_uid(replica_set, "rs-uid-1")], deployments=[deployment]
        )

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.workload_kind == WorkloadKind.DEPLOYMENT
        assert snapshot.workload_name == "test-deployment"
        assert len(snapshot.pod_uids) == 2  # both replicas collapsed into one workload
        assert snapshot.desired_replicas == 2
        assert snapshot.ready_replicas == 2
        assert snapshot.healthy is True
        container = snapshot.containers[0]
        assert container.requests.cpu_millicores == 500
        assert container.requests.memory_bytes == 256 * 1024 * 1024

    def test_resources_come_from_template_not_live_pod(self):
        # The live Pod's own spec has no resources at all (make_pod's
        # default container) -- requests/limits must still come from the
        # Deployment's template, proving the template is actually read.
        deployment = make_deployment(name="my-app", uid="deploy-uid-1")
        deployment.spec.template.spec = V1PodSpec(containers=[_container_with_resources(cpu_request="1")])
        replica_set = _with_uid(make_replica_set(name="my-app-abc123", owner_uid="deploy-uid-1"), "rs-uid-1")
        pod = _ready_pod("my-app-abc123-1", "pod-uid-1", owner=("ReplicaSet", "my-app-abc123", "rs-uid-1"))
        clients = _clients(pods=[pod], replica_sets=[replica_set], deployments=[deployment])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert snapshots[0].containers[0].requests.cpu_millicores == 1000

    def test_zero_ready_replicas_reports_as_zero_not_unknown(self):
        # Regression test: Kubernetes' `status.readyReplicas` is `omitempty`
        # -- confirmed live against a real broken Deployment, the field is
        # entirely absent from the API response when it's 0, which the
        # generated client deserializes as `None`. Must not be confused with
        # "unknown" (which `ready_replicas=None` means for a Job).
        deployment = make_deployment(name="test-deployment", uid="deploy-uid-1", desired_replicas=1)
        deployment.status.ready_replicas = None  # exactly what a real omitempty response deserializes to
        deployment.spec.template.spec = V1PodSpec(containers=[_container_with_resources()])
        replica_set = _with_uid(make_replica_set(name="my-app-abc123", owner_uid="deploy-uid-1"), "rs-uid-1")
        pod = make_pod(
            name="my-app-abc123-1", uid="pod-uid-1", owner=("ReplicaSet", "my-app-abc123", "rs-uid-1"),
            conditions=[V1PodCondition(type="Ready", status="False")],
        )
        clients = _clients(pods=[pod], replica_sets=[replica_set], deployments=[deployment])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert snapshots[0].ready_replicas == 0

    def test_unhealthy_when_restart_count_exceeds_threshold(self):
        deployment = make_deployment(name="my-app", uid="deploy-uid-1")
        deployment.spec.template.spec = V1PodSpec(containers=[_container_with_resources()])
        replica_set = _with_uid(make_replica_set(name="my-app-abc123", owner_uid="deploy-uid-1"), "rs-uid-1")
        pod = _ready_pod(
            "my-app-abc123-1", "pod-uid-1", owner=("ReplicaSet", "my-app-abc123", "rs-uid-1"), restart_count=10
        )
        clients = _clients(pods=[pod], replica_sets=[replica_set], deployments=[deployment])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert snapshots[0].healthy is False

    def test_missing_deployment_object_skips_workload_rather_than_guessing(self):
        # The Pod/ReplicaSet were listed but the Deployment itself wasn't
        # (simulating drift between the separate list calls) -- must be
        # skipped, never silently use the live Pod's spec as a stand-in.
        replica_set = _with_uid(make_replica_set(name="my-app-abc123", owner_uid="deploy-uid-missing"), "rs-uid-1")
        pod = _ready_pod("my-app-abc123-1", "pod-uid-1", owner=("ReplicaSet", "my-app-abc123", "rs-uid-1"))
        clients = _clients(pods=[pod], replica_sets=[replica_set], deployments=[])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert snapshots == []

    def test_system_namespace_flagged(self):
        deployment = make_deployment(name="coredns", uid="deploy-uid-1", namespace="kube-system")
        deployment.spec.template.spec = V1PodSpec(containers=[_container_with_resources()])
        replica_set = _with_uid(make_replica_set(name="coredns-abc", owner_uid="deploy-uid-1", namespace="kube-system"), "rs-uid-1")
        pod = make_pod(
            name="coredns-abc-1", uid="pod-uid-1", namespace="kube-system",
            owner=("ReplicaSet", "coredns-abc", "rs-uid-1"),
        )
        clients = _clients(pods=[pod], replica_sets=[replica_set], deployments=[deployment])

        snapshots = build_workload_inventory(
            clients, namespaces=["kube-system"], excluded_namespaces=["kube-system"]
        )

        assert snapshots[0].is_system_namespace is True


class TestStandalonePodInventory:
    def test_standalone_pod_is_its_own_workload(self):
        pod = _ready_pod("standalone", "pod-uid-standalone", owner=None)
        pod.spec = V1PodSpec(containers=[_container_with_resources(cpu_request="250m")])
        clients = _clients(pods=[pod])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert len(snapshots) == 1
        assert snapshots[0].workload_kind == WorkloadKind.POD
        assert snapshots[0].workload_name == "standalone"
        assert snapshots[0].desired_replicas == 1
        assert snapshots[0].ready_replicas == 1


class TestStatefulSetInventory:
    def test_statefulset_resolved_directly(self):
        sts = V1StatefulSet(
            metadata=V1ObjectMeta(name="my-sts", namespace=NAMESPACE, uid="sts-uid-1"),
            spec=V1StatefulSetSpec(
                replicas=1,
                selector=V1LabelSelector(match_labels={"app": "my-sts"}),
                service_name="my-sts",
                template=V1PodTemplateSpec(spec=V1PodSpec(containers=[_container_with_resources(memory_request="1Gi")])),
            ),
            status=V1StatefulSetStatus(replicas=1, ready_replicas=1),
        )
        pod = _ready_pod("my-sts-0", "pod-uid-1", owner=("StatefulSet", "my-sts", "sts-uid-1"))
        clients = _clients(pods=[pod], stateful_sets=[sts])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert len(snapshots) == 1
        assert snapshots[0].workload_kind == WorkloadKind.STATEFULSET
        assert snapshots[0].containers[0].requests.memory_bytes == 1024**3


class TestDaemonSetInventory:
    def test_daemonset_uses_desired_and_number_ready(self):
        ds = V1DaemonSet(
            metadata=V1ObjectMeta(name="my-ds", namespace=NAMESPACE, uid="ds-uid-1"),
            spec=V1DaemonSetSpec(
                selector=V1LabelSelector(match_labels={"app": "my-ds"}),
                template=V1PodTemplateSpec(spec=V1PodSpec(containers=[_container_with_resources()])),
            ),
            status=V1DaemonSetStatus(
                current_number_scheduled=1, desired_number_scheduled=3, number_misscheduled=0,
                number_ready=2,
            ),
        )
        pod = _ready_pod("my-ds-1", "pod-uid-1", owner=("DaemonSet", "my-ds", "ds-uid-1"))
        clients = _clients(pods=[pod], daemon_sets=[ds])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert snapshots[0].workload_kind == WorkloadKind.DAEMONSET
        assert snapshots[0].desired_replicas == 3
        assert snapshots[0].ready_replicas == 2


class TestJobInventory:
    def test_job_resolved_with_no_replica_counts(self):
        job = V1Job(
            metadata=V1ObjectMeta(name="my-job", namespace=NAMESPACE, uid="job-uid-1"),
            spec=V1JobSpec(template=V1PodTemplateSpec(spec=V1PodSpec(containers=[_container_with_resources()]))),
            status=V1JobStatus(succeeded=1),
        )
        pod = _ready_pod("my-job-xyz", "pod-uid-1", owner=("Job", "my-job", "job-uid-1"))
        clients = _clients(pods=[pod], jobs=[job])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert snapshots[0].workload_kind == WorkloadKind.JOB
        assert snapshots[0].desired_replicas is None
        assert snapshots[0].ready_replicas is None


class TestTerminatingPodsExcluded:
    def test_terminating_pod_is_excluded_entirely(self):
        pod = _ready_pod("going-away", "pod-uid-1", owner=None)
        pod.metadata.deletion_timestamp = "2026-01-01T00:00:00Z"
        clients = _clients(pods=[pod])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert snapshots == []


class TestOomDetection:
    def test_recent_oom_detected_from_last_state(self):
        pod = _ready_pod("standalone", "pod-uid-1", owner=None)
        pod.spec = V1PodSpec(containers=[_container_with_resources()])
        pod.status.container_statuses = [
            V1ContainerStatus(
                name="app",
                ready=True,
                restart_count=2,
                image="x",
                image_id="x",
                last_state=V1ContainerState(
                    terminated=V1ContainerStateTerminated(exit_code=137, reason="OOMKilled")
                ),
            )
        ]
        clients = _clients(pods=[pod])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert snapshots[0].containers[0].recent_oom_detected is True

    def test_no_oom_history_is_false(self):
        pod = _ready_pod("standalone", "pod-uid-1", owner=None)
        pod.spec = V1PodSpec(containers=[_container_with_resources()])
        clients = _clients(pods=[pod])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert snapshots[0].containers[0].recent_oom_detected is False

    def test_oom_on_different_container_name_does_not_leak_across_containers(self):
        pod = _ready_pod("standalone", "pod-uid-1", owner=None)
        pod.spec = V1PodSpec(containers=[_container_with_resources(name="app"), _container_with_resources(name="sidecar")])
        pod.status.container_statuses = [
            V1ContainerStatus(
                name="sidecar",
                ready=True,
                restart_count=2,
                image="x",
                image_id="x",
                last_state=V1ContainerState(
                    terminated=V1ContainerStateTerminated(exit_code=137, reason="OOMKilled")
                ),
            )
        ]
        clients = _clients(pods=[pod])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        by_name = {c.container_name: c for c in snapshots[0].containers}
        assert by_name["sidecar"].recent_oom_detected is True
        assert by_name["app"].recent_oom_detected is False


class TestUnschedulableReason:
    def test_unschedulable_reason_extracted_from_pod_scheduled_condition(self):
        pod = _ready_pod("standalone", "pod-uid-1", owner=None)
        pod.spec = V1PodSpec(containers=[_container_with_resources()])
        pod.status.conditions = [
            V1PodCondition(
                type="PodScheduled",
                status="False",
                reason="Unschedulable",
                message="0/1 nodes are available: 1 Insufficient cpu.",
            )
        ]
        clients = _clients(pods=[pod])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert snapshots[0].unschedulable_reason == "0/1 nodes are available: 1 Insufficient cpu."

    def test_schedulable_pod_has_no_unschedulable_reason(self):
        pod = _ready_pod("standalone", "pod-uid-1", owner=None)
        pod.spec = V1PodSpec(containers=[_container_with_resources()])
        clients = _clients(pods=[pod])

        snapshots = build_workload_inventory(clients, namespaces=[NAMESPACE])

        assert snapshots[0].unschedulable_reason is None


def _with_uid(obj, uid):
    obj.metadata.uid = uid
    return obj

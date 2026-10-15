"""Builders for Kubernetes client objects used across unit tests.

Using the real `kubernetes` client model classes (not hand-rolled dicts) means these
tests exercise the exact attribute shapes the inspector module reads in production.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kubernetes.client import (
    CoreV1Event,
    V1Container,
    V1ContainerState,
    V1ContainerStateRunning,
    V1ContainerStateTerminated,
    V1ContainerStateWaiting,
    V1ContainerStatus,
    V1Deployment,
    V1DeploymentCondition,
    V1DeploymentSpec,
    V1DeploymentStatus,
    V1LabelSelector,
    V1Node,
    V1NodeCondition,
    V1NodeSpec,
    V1NodeStatus,
    V1ObjectMeta,
    V1ObjectReference,
    V1OwnerReference,
    V1PersistentVolumeClaim,
    V1PersistentVolumeClaimStatus,
    V1Pod,
    V1PodCondition,
    V1PodSpec,
    V1PodStatus,
    V1PodTemplateSpec,
    V1ReplicaSet,
    V1ReplicaSetSpec,
    V1Service,
    V1ServiceSpec,
)


def make_replica_set(
    name: str = "test-deployment-abc123",
    namespace: str = "kubesentinel-lab",
    revision: str = "1",
    owner_uid: str = "deploy-uid-1234",
    image: str = "busybox:latest",
    owned: bool = True,
) -> V1ReplicaSet:
    owner_references = (
        [V1OwnerReference(api_version="apps/v1", kind="Deployment", name="test-deployment", uid=owner_uid)]
        if owned
        else []
    )
    return V1ReplicaSet(
        metadata=V1ObjectMeta(
            name=name,
            namespace=namespace,
            annotations={"deployment.kubernetes.io/revision": revision},
            owner_references=owner_references,
        ),
        spec=V1ReplicaSetSpec(
            selector=V1LabelSelector(match_labels={"app": "test-deployment"}),
            template=V1PodTemplateSpec(
                spec=V1PodSpec(containers=[V1Container(name="app", image=image)])
            ),
        ),
    )


def make_endpointslice(
    service_name: str = "test-service",
    namespace: str = "kubesentinel-lab",
    endpoint_count: int = 0,
    ready_count: int | None = None,
) -> dict:
    """Returns a plain dict, matching what EndpointSliceWatcher actually delivers
    in production (see observer/watchers.py::_ListWatchLoop's docstring: the typed
    V1EndpointSlice model raises on this resource's legitimate null `endpoints`
    field, so the real pipeline uses raw JSON dicts for it, not typed objects)."""
    ready_count = endpoint_count if ready_count is None else ready_count
    endpoints = [
        {"addresses": ["10.0.0.1"], "conditions": {"ready": i < ready_count}} for i in range(endpoint_count)
    ]
    return {
        "metadata": {
            "name": f"{service_name}-abcde",
            "namespace": namespace,
            "resourceVersion": "100",
            "labels": {"kubernetes.io/service-name": service_name},
        },
        "addressType": "IPv4",
        "endpoints": endpoints or None,  # None mirrors the real API's behavior for zero endpoints
    }


def make_service(
    name: str = "test-service",
    namespace: str = "kubesentinel-lab",
    uid: str = "svc-uid-1234",
    selector: dict | None = None,
) -> V1Service:
    return V1Service(
        metadata=V1ObjectMeta(name=name, namespace=namespace, uid=uid, resource_version="100"),
        spec=V1ServiceSpec(selector=selector if selector is not None else {"app": name}),
    )


def node_condition(condition_type: str, status: str, reason: str = "", message: str = "") -> V1NodeCondition:
    return V1NodeCondition(type=condition_type, status=status, reason=reason, message=message)


def make_node(
    name: str = "test-node",
    uid: str = "node-uid-1234",
    unschedulable: bool = False,
    conditions: list[V1NodeCondition] | None = None,
) -> V1Node:
    if conditions is None:
        conditions = [node_condition("Ready", "True")]
    return V1Node(
        metadata=V1ObjectMeta(name=name, uid=uid, resource_version="100"),
        spec=V1NodeSpec(unschedulable=unschedulable),
        status=V1NodeStatus(conditions=conditions),
    )


def make_pvc(
    name: str = "test-pvc",
    namespace: str = "kubesentinel-lab",
    uid: str = "pvc-uid-1234",
    phase: str = "Pending",
) -> V1PersistentVolumeClaim:
    return V1PersistentVolumeClaim(
        metadata=V1ObjectMeta(name=name, namespace=namespace, uid=uid, resource_version="100"),
        status=V1PersistentVolumeClaimStatus(phase=phase),
    )


def make_deployment(
    name: str = "test-deployment",
    namespace: str = "kubesentinel-lab",
    uid: str = "deploy-uid-1234",
    desired_replicas: int = 1,
    ready_replicas: int = 0,
    available_replicas: int = 0,
    updated_replicas: int = 0,
    unavailable_replicas: int = 0,
    conditions: list[V1DeploymentCondition] | None = None,
) -> V1Deployment:
    return V1Deployment(
        metadata=V1ObjectMeta(name=name, namespace=namespace, uid=uid, resource_version="100"),
        spec=V1DeploymentSpec(
            replicas=desired_replicas,
            selector=V1LabelSelector(match_labels={"app": name}),
            template=V1PodTemplateSpec(spec=V1PodSpec(containers=[V1Container(name="app")])),
        ),
        status=V1DeploymentStatus(
            ready_replicas=ready_replicas,
            available_replicas=available_replicas,
            updated_replicas=updated_replicas,
            unavailable_replicas=unavailable_replicas,
            conditions=conditions or [],
        ),
    )


def progressing_condition(status: str, reason: str, message: str = "") -> V1DeploymentCondition:
    return V1DeploymentCondition(type="Progressing", status=status, reason=reason, message=message)


def make_event(
    reason: str,
    message: str,
    event_type: str = "Warning",
    involved_object_kind: str = "Pod",
    involved_object_name: str = "test-pod",
    involved_object_uid: str = "uid-1234",
    namespace: str = "kubesentinel-lab",
    count: int = 1,
) -> CoreV1Event:
    return CoreV1Event(
        metadata=V1ObjectMeta(name="test-event", namespace=namespace),
        involved_object=V1ObjectReference(
            kind=involved_object_kind,
            name=involved_object_name,
            uid=involved_object_uid,
            namespace=namespace,
        ),
        reason=reason,
        message=message,
        type=event_type,
        count=count,
        last_timestamp=datetime.now(timezone.utc),
    )


def make_pod(
    name: str = "test-pod",
    namespace: str = "kubesentinel-lab",
    uid: str = "uid-1234",
    phase: str = "Running",
    container_statuses: list[V1ContainerStatus] | None = None,
    conditions: list[V1PodCondition] | None = None,
    owner: tuple[str, str, str] | None = None,
) -> V1Pod:
    owner_refs = None
    if owner:
        owner_kind, owner_name, owner_uid = owner
        owner_refs = [
            V1OwnerReference(api_version="apps/v1", kind=owner_kind, name=owner_name, uid=owner_uid)
        ]

    return V1Pod(
        metadata=V1ObjectMeta(
            name=name,
            namespace=namespace,
            uid=uid,
            resource_version="100",
            owner_references=owner_refs,
        ),
        spec=V1PodSpec(containers=[V1Container(name="app")]),
        status=V1PodStatus(
            phase=phase,
            container_statuses=container_statuses or [],
            conditions=conditions or [],
        ),
    )


def unschedulable_condition(message: str) -> V1PodCondition:
    return V1PodCondition(
        type="PodScheduled",
        status="False",
        reason="Unschedulable",
        message=message,
    )


def waiting_container_status(
    name: str = "app",
    reason: str = "CrashLoopBackOff",
    message: str = "back-off restarting failed container",
    restart_count: int = 5,
    last_terminated_reason: str | None = None,
    last_exit_code: int | None = None,
) -> V1ContainerStatus:
    last_state = V1ContainerState()
    if last_terminated_reason is not None:
        last_state.terminated = V1ContainerStateTerminated(
            reason=last_terminated_reason,
            exit_code=last_exit_code if last_exit_code is not None else 1,
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            finished_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        )

    return V1ContainerStatus(
        name=name,
        image="example/app:latest",
        image_id="",
        container_id="",
        ready=False,
        restart_count=restart_count,
        started=False,
        state=V1ContainerState(waiting=V1ContainerStateWaiting(reason=reason, message=message)),
        last_state=last_state,
    )


def mid_restart_terminated_container_status(
    name: str = "app",
    reason: str = "Error",
    exit_code: int = 1,
    restart_count: int = 4,
) -> V1ContainerStatus:
    """A container's CURRENT (not lastState) state is terminated, between restarts.

    This models the brief window right after a container exits and before the
    kubelet relabels it `waiting: CrashLoopBackOff` for the backoff period --
    distinct from `waiting_container_status`, which models the window *during* that
    backoff. Both are observable live; see health.py's module docstring.
    """
    return V1ContainerStatus(
        name=name,
        image="example/app:latest",
        image_id="",
        container_id="",
        ready=False,
        restart_count=restart_count,
        started=False,
        state=V1ContainerState(
            terminated=V1ContainerStateTerminated(
                reason=reason,
                exit_code=exit_code,
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                finished_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            )
        ),
        last_state=V1ContainerState(),
    )


def running_container_status(name: str = "app", restart_count: int = 0) -> V1ContainerStatus:
    return V1ContainerStatus(
        name=name,
        image="example/app:latest",
        image_id="",
        container_id="",
        ready=True,
        restart_count=restart_count,
        started=True,
        state=V1ContainerState(running=V1ContainerStateRunning(started_at=datetime.now(timezone.utc))),
        last_state=V1ContainerState(),
    )

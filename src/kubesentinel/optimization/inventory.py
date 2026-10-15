"""Builds `WorkloadSnapshot` objects from the live cluster.

One-shot, called by `OptimizationService.analyze()` (Milestone 5+) -- never a
watch/background loop, matching the design's explicit choice not to run an
always-on optimization thread (see RESOURCE_OPTIMIZATION_DESIGN.md §4).

Reads requests/limits from the owning workload's **own pod template**
(`Deployment`/`StatefulSet`/`DaemonSet`/`Job`'s `spec.template.spec`), never
from a live Pod's spec -- the spec's own "never edit an individual Pod when
its resource configuration belongs to a controller template" principle
applies just as much to *reading* as to writing: a live Pod should match its
template, but the template is the thing any future recommendation would
actually target, so it's the authoritative source now too. If a workload's
owning object can't be found (should not happen under normal collection, but
real cluster drift between list calls is possible), that workload is skipped
with a logged warning rather than silently falling back to a live Pod's spec
as a stand-in for the template.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kubesentinel.models.optimization import (
    ContainerResourceSnapshot,
    ResourceQuantity,
    WorkloadKind,
    WorkloadSnapshot,
)
from kubesentinel.observer.client import ClusterClients
from kubesentinel.optimization.ownership import ResolvedOwner, resolve_pod_owner
from kubesentinel.optimization.quantities import QuantityParseError, parse_cpu_to_millicores, parse_memory_to_bytes

logger = logging.getLogger(__name__)

# A workload whose Pods have restarted this many times (cumulative) is not
# "healthy" for optimization purposes -- deliberately conservative and
# independent of the troubleshooting engine's own incident thresholds
# (reusing diagnostics/rules/crash_loop.py's HIGH-confidence cutoff would
# couple the two engines where no coupling is needed; see
# RESOURCE_OPTIMIZATION_DESIGN.md §2 on keeping this a read-only consumer of
# incidents by ID, never of diagnostic logic).
_UNHEALTHY_RESTART_THRESHOLD = 5

_DIRECT_TEMPLATE_KINDS = {WorkloadKind.STATEFULSET, WorkloadKind.DAEMONSET, WorkloadKind.JOB}


def _list_all(list_namespaced: Callable[..., Any], list_all_namespaces: Callable[..., Any], namespaces: list[str]) -> list:
    if namespaces:
        items: list = []
        for namespace in namespaces:
            items.extend(list_namespaced(namespace=namespace).items)
        return items
    return list_all_namespaces().items


def _parse_resource_dict(raw: dict, *, context: str) -> ResourceQuantity:
    cpu_millicores = None
    memory_bytes = None
    if raw.get("cpu"):
        try:
            cpu_millicores = parse_cpu_to_millicores(raw["cpu"])
        except QuantityParseError:
            logger.warning("unparseable cpu quantity %r in %s", raw["cpu"], context)
    if raw.get("memory"):
        try:
            memory_bytes = parse_memory_to_bytes(raw["memory"])
        except QuantityParseError:
            logger.warning("unparseable memory quantity %r in %s", raw["memory"], context)
    return ResourceQuantity(cpu_millicores=cpu_millicores, memory_bytes=memory_bytes)


def _container_oom_detected(group_pods: list, container_name: str) -> bool:
    for pod in group_pods:
        for container_status in (pod.status.container_statuses if pod.status else None) or []:
            if container_status.name != container_name:
                continue
            for state in (container_status.last_state, container_status.state):
                if state and state.terminated and state.terminated.reason == "OOMKilled":
                    return True
    return False


def _unschedulable_reason(group_pods: list) -> str | None:
    for pod in group_pods:
        for condition in (pod.status.conditions if pod.status else None) or []:
            if condition.type == "PodScheduled" and condition.status == "False" and condition.reason == "Unschedulable":
                return condition.message or ""
    return None


def _container_snapshots(pod_spec, group_pods: list, *, context: str) -> list[ContainerResourceSnapshot]:
    snapshots = []
    for container in pod_spec.containers or []:
        requests = ResourceQuantity()
        limits = ResourceQuantity()
        resources = container.resources
        if resources is not None:
            if resources.requests:
                requests = _parse_resource_dict(resources.requests, context=f"{context}/{container.name} requests")
            if resources.limits:
                limits = _parse_resource_dict(resources.limits, context=f"{context}/{container.name} limits")
        snapshots.append(
            ContainerResourceSnapshot(
                container_name=container.name,
                requests=requests,
                limits=limits,
                recent_oom_detected=_container_oom_detected(group_pods, container.name),
            )
        )
    return snapshots


def _is_pod_ready(pod) -> bool:
    if pod.status is None or pod.status.phase != "Running":
        return False
    for condition in pod.status.conditions or []:
        if condition.type == "Ready":
            return condition.status == "True"
    return False


def _group_is_healthy(pods: list) -> bool:
    if not pods:
        return False
    for pod in pods:
        if not _is_pod_ready(pod):
            return False
        for container_status in pod.status.container_statuses or []:
            if container_status.restart_count is not None and container_status.restart_count >= _UNHEALTHY_RESTART_THRESHOLD:
                return False
            last_state = container_status.last_state
            if last_state and last_state.terminated and last_state.terminated.reason == "OOMKilled":
                return False
    return True


def _replica_counts(kind: WorkloadKind, controller_obj) -> tuple[int | None, int | None]:
    """`status.readyReplicas`/`status.numberReady` are `omitempty` in the
    Kubernetes API -- confirmed live against a real broken Deployment in
    this project's lab: the field is entirely *absent* from the JSON when
    its value is 0, not present-and-zero. The generated client then
    deserializes that absence as `None`, which this project's own
    `WorkloadSnapshot.ready_replicas` uses to mean "unknown" -- left
    uncorrected, every genuinely-zero-ready workload would misreport as
    "ready replicas unknown" instead of the true, more informative "0 of N
    ready." `spec.replicas` has no such omitempty behavior (Kubernetes
    defaults it to 1 when unset, and it is always present on a real object),
    so only the ready-count side needs this normalization."""
    if controller_obj is None:
        return None, None
    if kind in (WorkloadKind.DEPLOYMENT, WorkloadKind.STATEFULSET):
        return controller_obj.spec.replicas, controller_obj.status.ready_replicas or 0
    if kind == WorkloadKind.DAEMONSET:
        return controller_obj.status.desired_number_scheduled, controller_obj.status.number_ready or 0
    return None, None  # Job: "replicas" doesn't map cleanly onto completions semantics


def build_workload_inventory(
    clients: ClusterClients,
    namespaces: list[str] | None = None,
    excluded_namespaces: list[str] | None = None,
) -> list[WorkloadSnapshot]:
    namespaces = namespaces or []
    excluded = set(excluded_namespaces or [])

    pods = _list_all(clients.core.list_namespaced_pod, clients.core.list_pod_for_all_namespaces, namespaces)
    replica_sets = _list_all(
        clients.apps.list_namespaced_replica_set, clients.apps.list_replica_set_for_all_namespaces, namespaces
    )
    deployments = _list_all(
        clients.apps.list_namespaced_deployment, clients.apps.list_deployment_for_all_namespaces, namespaces
    )
    stateful_sets = _list_all(
        clients.apps.list_namespaced_stateful_set, clients.apps.list_stateful_set_for_all_namespaces, namespaces
    )
    daemon_sets = _list_all(
        clients.apps.list_namespaced_daemon_set, clients.apps.list_daemon_set_for_all_namespaces, namespaces
    )
    jobs = _list_all(clients.batch.list_namespaced_job, clients.batch.list_job_for_all_namespaces, namespaces)

    replicasets_by_uid = {rs.metadata.uid: rs for rs in replica_sets}
    controllers_by_kind_and_uid: dict[WorkloadKind, dict[str, Any]] = {
        WorkloadKind.DEPLOYMENT: {d.metadata.uid: d for d in deployments},
        WorkloadKind.STATEFULSET: {s.metadata.uid: s for s in stateful_sets},
        WorkloadKind.DAEMONSET: {d.metadata.uid: d for d in daemon_sets},
        WorkloadKind.JOB: {j.metadata.uid: j for j in jobs},
    }

    groups: dict[tuple[WorkloadKind, str], dict[str, Any]] = {}
    unresolved_count = 0
    for pod in pods:
        if pod.metadata.deletion_timestamp is not None:
            # Same principle as observer/service.py::_handle_pod_event: a
            # terminating Pod's transient status is not a source of
            # information about the workload's steady-state resource needs.
            continue
        owner: ResolvedOwner | None = resolve_pod_owner(pod, replicasets_by_uid)
        if owner is None:
            unresolved_count += 1
            continue
        key = (owner.kind, owner.uid)
        group = groups.setdefault(
            key, {"name": owner.name, "namespace": pod.metadata.namespace, "pods": []}
        )
        group["pods"].append(pod)

    if unresolved_count:
        logger.warning(
            "could not resolve an owning workload for %d pod(s) during inventory build "
            "(its controller may not have been listed in this same pass)",
            unresolved_count,
        )

    snapshots: list[WorkloadSnapshot] = []
    for (kind, uid), group in groups.items():
        namespace = group["namespace"]
        name = group["name"]
        group_pods = group["pods"]
        context = f"{namespace}/{kind.value}/{name}"

        if kind == WorkloadKind.POD:
            pod_spec = group_pods[0].spec if group_pods else None
            controller_obj = None
        else:
            controller_obj = controllers_by_kind_and_uid.get(kind, {}).get(uid)
            if controller_obj is None:
                logger.warning(
                    "skipping %s: owning %s object (uid=%s) was not found in this inventory "
                    "pass -- never substituting a live Pod's spec for the template",
                    context,
                    kind.value,
                    uid,
                )
                continue
            pod_spec = controller_obj.spec.template.spec

        if pod_spec is None:
            logger.warning("skipping %s: no pod template spec available", context)
            continue

        desired_replicas, ready_replicas = (
            (1, 1 if group_pods and _is_pod_ready(group_pods[0]) else 0)
            if kind == WorkloadKind.POD
            else _replica_counts(kind, controller_obj)
        )

        snapshots.append(
            WorkloadSnapshot(
                namespace=namespace,
                workload_kind=kind,
                workload_name=name,
                workload_uid=uid,
                containers=_container_snapshots(pod_spec, group_pods, context=context),
                desired_replicas=desired_replicas,
                ready_replicas=ready_replicas,
                pod_uids=[p.metadata.uid for p in group_pods],
                pod_names=[p.metadata.name for p in group_pods],
                healthy=_group_is_healthy(group_pods),
                is_system_namespace=namespace in excluded,
                unschedulable_reason=_unschedulable_reason(group_pods),
            )
        )

    return snapshots

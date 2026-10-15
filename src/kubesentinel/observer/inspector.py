"""Pure functions that turn raw Kubernetes API objects into Evidence.

Kept free of any network or watch logic so diagnostic rules and this extraction logic
can be unit tested with hand-built client objects, no live cluster required.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kubernetes.client import (
    CoreV1Event,
    V1Deployment,
    V1Node,
    V1PersistentVolumeClaim,
    V1Pod,
    V1Service,
)

from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.models.incident import ResourceRef

_now = lambda: datetime.now(timezone.utc)  # noqa: E731


def resource_ref_from_pod(pod: V1Pod) -> ResourceRef:
    meta = pod.metadata
    owner_refs = meta.owner_references or []
    owner = owner_refs[0] if owner_refs else None
    return ResourceRef(
        namespace=meta.namespace,
        kind="Pod",
        name=meta.name,
        uid=meta.uid,
        owner_kind=owner.kind if owner else None,
        owner_name=owner.name if owner else None,
        owner_uid=owner.uid if owner else None,
    )


def pod_evidence(pod: V1Pod) -> list[Evidence]:
    """Extract all evidence this module knows how to read from a single Pod object."""
    evidence: list[Evidence] = []
    observed_at = _now()
    resource_version = pod.metadata.resource_version if pod.metadata else None

    if pod.status is None:
        return evidence

    evidence.append(
        Evidence(
            kind=EvidenceKind.POD_STATUS,
            observed_at=observed_at,
            source_field="status.phase",
            summary=f"pod phase is {pod.status.phase}",
            data={"phase": pod.status.phase, "reason": pod.status.reason, "message": pod.status.message},
            resource_version=resource_version,
        )
    )

    for cond in pod.status.conditions or []:
        evidence.append(
            Evidence(
                kind=EvidenceKind.POD_CONDITION,
                observed_at=observed_at,
                source_field=f"status.conditions[{cond.type}]",
                summary=f"condition {cond.type}={cond.status} ({cond.reason or 'no reason'})",
                data={
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason,
                    "message": cond.message,
                },
                resource_version=resource_version,
            )
        )

    for statuses, label in (
        (pod.status.container_statuses, "containerStatuses"),
        (pod.status.init_container_statuses, "initContainerStatuses"),
    ):
        for cs in statuses or []:
            evidence.extend(_container_status_evidence(cs, label, observed_at, resource_version))

    return evidence


def _container_status_evidence(cs, label: str, observed_at: datetime, resource_version: str | None):
    out: list[Evidence] = []
    base_data = {
        "container": cs.name,
        "restart_count": cs.restart_count,
        "ready": cs.ready,
        "started": cs.started,
    }

    out.append(
        Evidence(
            kind=EvidenceKind.CONTAINER_STATE,
            observed_at=observed_at,
            source_field=f"{label}[{cs.name}].restartCount",
            summary=f"container '{cs.name}' restart_count={cs.restart_count} ready={cs.ready}",
            data=dict(base_data),
            resource_version=resource_version,
        )
    )

    state = cs.state
    if state is None:
        return out

    if state.waiting is not None:
        out.append(
            Evidence(
                kind=EvidenceKind.CONTAINER_STATE,
                observed_at=observed_at,
                source_field=f"{label}[{cs.name}].state.waiting",
                summary=f"container '{cs.name}' waiting: {state.waiting.reason}",
                data={
                    **base_data,
                    "waiting_reason": state.waiting.reason,
                    "waiting_message": state.waiting.message,
                },
                resource_version=resource_version,
            )
        )

    if state.terminated is not None:
        t = state.terminated
        out.append(
            Evidence(
                kind=EvidenceKind.CONTAINER_TERMINATION,
                observed_at=observed_at,
                source_field=f"{label}[{cs.name}].state.terminated",
                summary=(
                    f"container '{cs.name}' terminated: reason={t.reason} exit_code={t.exit_code}"
                ),
                data={
                    **base_data,
                    "terminated_reason": t.reason,
                    "exit_code": t.exit_code,
                    "signal": t.signal,
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                    "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                },
                resource_version=resource_version,
            )
        )

    last_state = cs.last_state
    if last_state is not None and last_state.terminated is not None:
        t = last_state.terminated
        out.append(
            Evidence(
                kind=EvidenceKind.CONTAINER_TERMINATION,
                observed_at=observed_at,
                source_field=f"{label}[{cs.name}].lastState.terminated",
                summary=(
                    f"container '{cs.name}' PREVIOUS termination: reason={t.reason} "
                    f"exit_code={t.exit_code}"
                ),
                data={
                    **base_data,
                    "last_terminated_reason": t.reason,
                    "last_exit_code": t.exit_code,
                    "last_signal": t.signal,
                },
                resource_version=resource_version,
            )
        )

    return out


def resource_ref_from_deployment(deployment: V1Deployment) -> ResourceRef:
    meta = deployment.metadata
    return ResourceRef(namespace=meta.namespace, kind="Deployment", name=meta.name, uid=meta.uid)


def deployment_evidence(deployment: V1Deployment) -> list[Evidence]:
    """Extract replica counts and rollout conditions from a Deployment object."""
    evidence: list[Evidence] = []
    observed_at = _now()
    resource_version = deployment.metadata.resource_version if deployment.metadata else None
    status = deployment.status
    if status is None:
        return evidence

    desired = deployment.spec.replicas if deployment.spec and deployment.spec.replicas is not None else 1
    ready = status.ready_replicas or 0
    available = status.available_replicas or 0
    updated = status.updated_replicas or 0
    unavailable = status.unavailable_replicas or 0

    evidence.append(
        Evidence(
            kind=EvidenceKind.DEPLOYMENT_STATUS,
            observed_at=observed_at,
            source_field="status.replicas",
            summary=f"deployment replicas: desired={desired} ready={ready} available={available}",
            data={
                "desired_replicas": desired,
                "ready_replicas": ready,
                "available_replicas": available,
                "updated_replicas": updated,
                "unavailable_replicas": unavailable,
            },
            resource_version=resource_version,
        )
    )

    for cond in status.conditions or []:
        evidence.append(
            Evidence(
                kind=EvidenceKind.DEPLOYMENT_STATUS,
                observed_at=observed_at,
                source_field=f"status.conditions[{cond.type}]",
                summary=f"condition {cond.type}={cond.status} ({cond.reason or 'no reason'})",
                data={
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason,
                    "message": cond.message,
                },
                resource_version=resource_version,
            )
        )

    return evidence


_EVENT_RESOURCE_KINDS = {"Pod", "PersistentVolumeClaim"}


def resource_ref_from_event(event: CoreV1Event) -> ResourceRef | None:
    """Build a ResourceRef for the object an Event is about, for the kinds this
    project correlates events against (Pod, PersistentVolumeClaim).

    Events don't carry the involved object's ownerReferences (only the object
    itself does), so owner_kind/owner_name/owner_uid are left unset here -- a
    deliberate gap, not an oversight: correlating an event-only observation up to an
    owner would need a live GET of the object, which this function (kept pure, no
    API calls) doesn't do.
    """
    obj = event.involved_object
    if obj is None or obj.kind not in _EVENT_RESOURCE_KINDS or not obj.uid:
        return None
    namespace = obj.namespace or (event.metadata.namespace if event.metadata else None)
    if not namespace:
        return None
    return ResourceRef(namespace=namespace, kind=obj.kind, name=obj.name, uid=obj.uid)


def resource_ref_from_node(node: V1Node) -> ResourceRef:
    meta = node.metadata
    # Node is cluster-scoped -- there is no real namespace. "cluster-scoped" is used
    # as a readable placeholder so ResourceRef's required `namespace: str` field
    # doesn't need to become optional just for this one resource kind.
    return ResourceRef(namespace="cluster-scoped", kind="Node", name=meta.name, uid=meta.uid)


def node_evidence(node: V1Node) -> list[Evidence]:
    """Extract a Node's own health conditions and cordon state."""
    evidence: list[Evidence] = []
    observed_at = _now()
    resource_version = node.metadata.resource_version if node.metadata else None

    if node.spec is not None:
        evidence.append(
            Evidence(
                kind=EvidenceKind.NODE_CONDITION,
                observed_at=observed_at,
                source_field="spec.unschedulable",
                summary=f"node unschedulable (cordoned) = {bool(node.spec.unschedulable)}",
                data={"unschedulable": bool(node.spec.unschedulable)},
                resource_version=resource_version,
            )
        )

    if node.status is not None:
        for cond in node.status.conditions or []:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.NODE_CONDITION,
                    observed_at=observed_at,
                    source_field=f"status.conditions[{cond.type}]",
                    summary=f"condition {cond.type}={cond.status} ({cond.reason or 'no reason'})",
                    data={
                        "type": cond.type,
                        "status": cond.status,
                        "reason": cond.reason,
                        "message": cond.message,
                    },
                    resource_version=resource_version,
                )
            )

    return evidence


def resource_ref_from_service(service: V1Service) -> ResourceRef:
    meta = service.metadata
    return ResourceRef(namespace=meta.namespace, kind="Service", name=meta.name, uid=meta.uid)


def endpointslice_evidence(endpointslice: dict) -> list[Evidence]:
    """Extract the ready/total endpoint counts from one EndpointSlice.

    Takes a plain dict, not a typed `V1EndpointSlice` -- see
    `observer/watchers.py::_ListWatchLoop`'s docstring for why: the generated
    client's typed model for this resource raises on a genuinely empty (JSON
    `null`) `endpoints` field, which the API server sends for a zero-endpoint
    slice -- confirmed live. `EndpointSliceWatcher` fetches raw JSON specifically
    to avoid that, so this function (and resource_ref_from_event's caller for it)
    works on dicts shaped like the raw Kubernetes API response.

    A Service can own multiple EndpointSlices; ObserverService is responsible for
    summing across all of a Service's slices before calling this -- this function
    only describes a single slice object.
    """
    endpoints = endpointslice.get("endpoints") or []
    ready = sum(
        1 for ep in endpoints if (ep.get("conditions") or {}).get("ready") is not False
    )
    metadata = endpointslice.get("metadata") or {}
    return [
        Evidence(
            kind=EvidenceKind.ENDPOINTSLICE_STATE,
            observed_at=_now(),
            source_field="endpoints",
            summary=f"endpointslice has {len(endpoints)} endpoints ({ready} ready)",
            data={"total_endpoints": len(endpoints), "ready_endpoints": ready},
            resource_version=metadata.get("resourceVersion"),
        )
    ]


def resource_ref_from_pvc(pvc: V1PersistentVolumeClaim) -> ResourceRef:
    meta = pvc.metadata
    return ResourceRef(namespace=meta.namespace, kind="PersistentVolumeClaim", name=meta.name, uid=meta.uid)


def pvc_evidence(pvc: V1PersistentVolumeClaim) -> list[Evidence]:
    """Extract the PVC's own phase. Binding *failure* detail only ever shows up via
    Events (ProvisioningFailed, FailedBinding) -- see pvc.py's module docstring for
    why `Pending` phase alone is deliberately not treated as a failure signal."""
    if pvc.status is None:
        return []
    return [
        Evidence(
            kind=EvidenceKind.PVC_STATUS,
            observed_at=_now(),
            source_field="status.phase",
            summary=f"PVC phase is {pvc.status.phase}",
            data={"phase": pvc.status.phase},
            resource_version=pvc.metadata.resource_version if pvc.metadata else None,
        )
    ]


def event_evidence(event: CoreV1Event) -> Evidence:
    last_ts = event.last_timestamp or event.event_time or event.first_timestamp
    return Evidence(
        kind=EvidenceKind.EVENT,
        observed_at=last_ts or _now(),
        source_field="event",
        summary=f"[{event.type}] {event.reason}: {event.message}",
        data={
            "type": event.type,
            "reason": event.reason,
            "message": event.message,
            "count": event.count,
            "involved_object_kind": event.involved_object.kind if event.involved_object else None,
            "involved_object_name": event.involved_object.name if event.involved_object else None,
        },
    )

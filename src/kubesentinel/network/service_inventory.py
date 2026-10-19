"""Builds the Service-side state the network engine needs: which Pods a
Service's selector actually matches (computed independently, not inferred
from endpoint counts), whether the real EndpointSlice controller output
agrees, whether each `targetPort` actually resolves against the matched
Pods' declared container ports, and -- closing this engine's own
previously-named "Service types / routing: Partial" gap -- the Service's
own `type` and, for `LoadBalancer`, its real external ingress assignment.
`NodePort`/`LoadBalancer` need no separate routing logic of their own:
both are strict supersets of `ClusterIP` (the selector/EndpointSlice/port
checks above already cover them identically), so this is additive
visibility onto the existing checks, not a parallel code path.

Read-only: only ever `list_namespaced_service`/`list_namespaced_pod` plus a
raw-JSON EndpointSlice LIST (see `_list_endpointslices_raw`'s own
docstring). No watch loop -- a one-shot snapshot per `analyze()` call, the
same shape `optimization/inventory.py`/`scheduling/node_inventory.py`
already use.
"""

from __future__ import annotations

import json

from kubernetes.client import V1Pod, V1Service

from kubesentinel.models.network import PortMatchResult, ServiceMatchResult
from kubesentinel.observer.client import ClusterClients


def _list_endpointslices_raw(clients: ClusterClients, namespace: str) -> list[dict]:
    """Same raw-JSON LIST technique `observer/watchers.py::_ListWatchLoop`
    uses for its continuous watch, applied here to a one-shot snapshot: the
    generated client's typed `V1EndpointSlice` model raises on a
    genuinely-empty (`null`) `endpoints` field, which the API server
    legitimately sends for a zero-endpoint slice (Go's `omitempty` on a nil
    slice) -- confirmed live against this project's own kind cluster. Fetch
    raw JSON (`_preload_content=False`) and skip the typed deserializer
    entirely, exactly like the watcher does."""
    raw = clients.discovery.list_namespaced_endpoint_slice(namespace=namespace, _preload_content=False)
    data = json.loads(raw.data)
    return data.get("items", [])


def _pod_matches_selector(pod: V1Pod, selector: dict) -> bool:
    if not selector:
        return False
    labels = pod.metadata.labels or {}
    return all(labels.get(k) == v for k, v in selector.items())


def _pod_is_ready(pod: V1Pod) -> bool:
    for condition in pod.status.conditions or []:
        if condition.type == "Ready":
            return condition.status == "True"
    return False


def _resolve_target_port(target_port: str, representative_pod: V1Pod | None) -> PortMatchResult:
    """`target_port` is always stringified before this is called (a numeric
    `targetPort` is serialized as a plain int in the Service spec; a named
    one as a string -- normalized to `str` by the caller so this function
    has one input shape).

    Kubernetes' own semantics are asymmetric and worth getting exactly
    right, not just approximated: a **numeric** `targetPort` routes to that
    port number on the Pod's network namespace whether or not any
    container *declares* it in `ports:` -- declaration is purely
    informational for a numeric target. A **named** `targetPort` has no
    such fallback: Kubernetes requires it to match a real, declared
    `containerPort.name` on the Pod, or that Service port has no usable
    endpoint on that Pod at all. The two cases are therefore reported with
    different confidence, not collapsed into one "matched" bool."""
    is_numeric = target_port.lstrip("-").isdigit()
    if representative_pod is not None:
        for container in representative_pod.spec.containers or []:
            for port in container.ports or []:
                if is_numeric and port.container_port == int(target_port):
                    return PortMatchResult(
                        service_port=0,  # overwritten by the caller, which knows the real service_port
                        target_port=target_port,
                        matched_container_port=port.container_port,
                        matched=True,
                        reasoning=f"targetPort {target_port} matches a declared containerPort on '{container.name}'",
                    )
                if not is_numeric and port.name == target_port:
                    return PortMatchResult(
                        service_port=0,
                        target_port=target_port,
                        matched_container_port=port.container_port,
                        matched=True,
                        reasoning=f"named targetPort '{target_port}' resolves to containerPort {port.container_port} on '{container.name}'",
                    )
    if is_numeric:
        return PortMatchResult(
            service_port=0,
            target_port=target_port,
            matched_container_port=int(target_port),
            matched=True,
            reasoning=(
                f"targetPort {target_port} is numeric -- Kubernetes routes to this port on the Pod's "
                "network namespace whether or not a container declares it, so this is still structurally "
                "valid even though no container explicitly declares this port"
            ),
        )
    return PortMatchResult(
        service_port=0,
        target_port=target_port,
        matched_container_port=None,
        matched=False,
        reasoning=(
            f"named targetPort '{target_port}' does not match any declared containerPort name on the "
            "matched Pod(s) -- Kubernetes requires a named targetPort to resolve to a real container "
            "port name, so this Service port will not route correctly"
        ),
    )


def build_service_match_results(clients: ClusterClients, namespace: str) -> list[ServiceMatchResult]:
    services = clients.core.list_namespaced_service(namespace=namespace).items
    pods = clients.core.list_namespaced_pod(namespace=namespace).items
    endpointslices = _list_endpointslices_raw(clients, namespace)

    results = []
    for svc in services:
        results.append(_match_one_service(svc, pods, endpointslices))
    return results


def _match_one_service(svc: V1Service, pods: list[V1Pod], endpointslices: list[dict]) -> ServiceMatchResult:
    selector = dict(svc.spec.selector or {})
    matched_pods = [p for p in pods if _pod_matches_selector(p, selector)]
    matched_pod_names = [p.metadata.name for p in matched_pods]
    ready_pod_names = [p.metadata.name for p in matched_pods if _pod_is_ready(p)]

    owned_slices = [
        s
        for s in endpointslices
        if (s.get("metadata", {}).get("labels", {}) or {}).get("kubernetes.io/service-name") == svc.metadata.name
    ]
    endpoint_count = 0
    ready_count = 0
    for slice_dict in owned_slices:
        endpoints = slice_dict.get("endpoints") or []
        endpoint_count += len(endpoints)
        ready_count += sum(1 for ep in endpoints if (ep.get("conditions") or {}).get("ready") is not False)

    # The EndpointSlice controller's own job is to keep its output in sync
    # with the Service's selector -- these should always agree. Comparing
    # ready-Pod counts (not raw endpoint counts) against ready-endpoint
    # counts is the correct apples-to-apples check.
    endpointslice_consistent = len(ready_pod_names) == ready_count if selector else True

    representative_pod = matched_pods[0] if matched_pods else None
    port_results = []
    for svc_port in svc.spec.ports or []:
        target_port = str(svc_port.target_port) if svc_port.target_port is not None else str(svc_port.port)
        result = _resolve_target_port(target_port, representative_pod)
        result.service_port = svc_port.port
        result.service_port_name = svc_port.name
        result.node_port = svc_port.node_port
        port_results.append(result)

    service_type = svc.spec.type or "ClusterIP"
    load_balancer_ingress: list[str] = []
    if service_type == "LoadBalancer" and svc.status and svc.status.load_balancer and svc.status.load_balancer.ingress:
        load_balancer_ingress = [
            ingress.hostname or ingress.ip for ingress in svc.status.load_balancer.ingress if ingress.hostname or ingress.ip
        ]

    return ServiceMatchResult(
        service_name=svc.metadata.name,
        selector=selector,
        matched_pod_names=matched_pod_names,
        ready_pod_names=ready_pod_names,
        endpointslice_endpoint_count=endpoint_count,
        endpointslice_ready_count=ready_count,
        endpointslice_consistent=endpointslice_consistent,
        port_results=port_results,
        service_type=service_type,
        load_balancer_ingress=load_balancer_ingress,
    )

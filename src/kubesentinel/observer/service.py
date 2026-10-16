"""Wires the Pod, Event, and Deployment watchers to evidence extraction and the
incident manager."""

from __future__ import annotations

import logging
import threading

from kubernetes.client import (
    CoreV1Event,
    V1Deployment,
    V1Node,
    V1PersistentVolumeClaim,
    V1Pod,
)
from kubernetes.client.rest import ApiException

from kubesentinel.config import Settings
from kubesentinel.incidents.manager import IncidentManager
from kubesentinel.metrics.registry import observer_errors_total
from kubesentinel.observer.client import ClusterClients
from kubesentinel.observer.inspector import (
    deployment_evidence,
    endpointslice_evidence,
    event_evidence,
    node_evidence,
    pod_evidence,
    pvc_evidence,
    resource_ref_from_deployment,
    resource_ref_from_event,
    resource_ref_from_node,
    resource_ref_from_pod,
    resource_ref_from_pvc,
    resource_ref_from_service,
)
from kubesentinel.models.incident import ResourceRef
from kubesentinel.observer.watchers import (
    DeploymentWatcher,
    EndpointSliceWatcher,
    EventType,
    EventWatcher,
    NodeWatcher,
    PodWatcher,
    PVCWatcher,
    Watcher,
)

logger = logging.getLogger(__name__)

# Event reasons worth forwarding to the incident manager at all. Most Events are
# routine (Scheduled, Pulled, Created, Started) and carry no diagnostic value beyond
# what Pod/PVC status evidence already provides -- filtering here keeps evidence
# lists from filling up with noise that no rule reads. Limited to exactly what
# ReadinessProbeFailingRule/LivenessProbeFailingRule/PvcBindingFailureRule consume
# today; extend this set only alongside a rule that actually reads the new reason.
_RELEVANT_EVENT_REASONS = {"Unhealthy", "ProvisioningFailed", "FailedBinding", "FailedMount"}


class ObserverService:
    def __init__(self, clients: ClusterClients, settings: Settings, manager: IncidentManager):
        self._manager = manager
        self._core = clients.core
        watchers: dict[str, Watcher] = {
            "pod-watcher": PodWatcher(
                core_api=clients.core,
                namespaces=settings.watch_namespace_list,
                on_pod_event=self._handle_pod_event,
                resync_interval_seconds=settings.resync_interval_seconds,
            ),
            "event-watcher": EventWatcher(
                core_api=clients.core,
                namespaces=settings.watch_namespace_list,
                on_event=self._handle_event,
                resync_interval_seconds=settings.resync_interval_seconds,
            ),
            "deployment-watcher": DeploymentWatcher(
                apps_api=clients.apps,
                namespaces=settings.watch_namespace_list,
                on_deployment_event=self._handle_deployment_event,
                resync_interval_seconds=settings.resync_interval_seconds,
            ),
            "pvc-watcher": PVCWatcher(
                core_api=clients.core,
                namespaces=settings.watch_namespace_list,
                on_pvc_event=self._handle_pvc_event,
                resync_interval_seconds=settings.resync_interval_seconds,
            ),
            "endpointslice-watcher": EndpointSliceWatcher(
                discovery_api=clients.discovery,
                namespaces=settings.watch_namespace_list,
                on_endpointslice_event=self._handle_endpointslice_event,
                resync_interval_seconds=settings.resync_interval_seconds,
            ),
        }
        if settings.watch_nodes:
            watchers["node-watcher"] = NodeWatcher(
                core_api=clients.core,
                on_node_event=self._handle_node_event,
                resync_interval_seconds=settings.resync_interval_seconds,
            )
        self._watchers = watchers
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    def _handle_pod_event(self, event_type: EventType, pod: V1Pod) -> None:
        if event_type == "DELETED" or pod.metadata is None:
            return
        if pod.metadata.deletion_timestamp is not None:
            # A terminating Pod's container statuses can transiently show no bad
            # waiting/termination reason while it's being torn down (e.g. statuses
            # cleared before the container object disappears). That is not evidence of
            # recovery -- skip it entirely rather than risk resolving a live incident
            # on a pod that is simply going away, not getting healthier.
            return
        try:
            ref = resource_ref_from_pod(pod)
            evidence = pod_evidence(pod)
            self._manager.observe(ref, evidence)
        except Exception:  # noqa: BLE001 - one bad pod must not kill the watch loop
            observer_errors_total.labels(component="pod_watcher").inc()
            logger.exception("failed handling pod event", extra={"pod": pod.metadata.name})

    def _handle_event(self, event_type: EventType, event: CoreV1Event) -> None:
        if event_type == "DELETED" or event.reason not in _RELEVANT_EVENT_REASONS:
            return
        try:
            ref = resource_ref_from_event(event)
            if ref is None:
                return
            # Events outlive the resource they describe -- their own GC runs on a
            # separate, longer TTL than the resource's deletion. Found live: a
            # ProvisioningFailed Event for an already-deleted PVC was still present
            # on process restart and would otherwise open a phantom incident for a
            # PVC that no longer exists. Confirm the object is still there first.
            if not self._still_exists(ref):
                return
            self._manager.observe(ref, [event_evidence(event)])
        except Exception:  # noqa: BLE001 - one bad event must not kill the watch loop
            observer_errors_total.labels(component="event_watcher").inc()
            logger.exception("failed handling event", extra={"reason": event.reason})

    def _still_exists(self, ref: ResourceRef) -> bool:
        """True only if the exact object `ref` describes (by UID) is still live.

        Checking by name alone is not enough: found live immediately after fixing
        the "Events outlive the resource" bug above -- a stale Event about a
        deleted PVC was "revalidated" because a *different* PVC with the same name
        had since been created (the exact same name-reuse scenario this project's
        UID-based incident dedup exists to handle correctly elsewhere). A name
        match on an object with a different UID is exactly as stale as no object
        at all -- the thing the Event was about really is gone.
        """
        try:
            if ref.kind == "Pod":
                obj = self._core.read_namespaced_pod(name=ref.name, namespace=ref.namespace)
            elif ref.kind == "PersistentVolumeClaim":
                obj = self._core.read_namespaced_persistent_volume_claim(name=ref.name, namespace=ref.namespace)
            else:
                return True  # no existence check implemented for this kind; assume yes
            return obj.metadata.uid == ref.uid
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise

    def _handle_deployment_event(self, event_type: EventType, deployment: V1Deployment) -> None:
        if event_type == "DELETED" or deployment.metadata is None:
            return
        try:
            ref = resource_ref_from_deployment(deployment)
            evidence = deployment_evidence(deployment)
            self._manager.observe(ref, evidence)
        except Exception:  # noqa: BLE001 - one bad deployment must not kill the watch loop
            observer_errors_total.labels(component="deployment_watcher").inc()
            logger.exception(
                "failed handling deployment event", extra={"deployment": deployment.metadata.name}
            )

    def _handle_pvc_event(self, event_type: EventType, pvc: V1PersistentVolumeClaim) -> None:
        if event_type == "DELETED" or pvc.metadata is None:
            return
        try:
            ref = resource_ref_from_pvc(pvc)
            evidence = pvc_evidence(pvc)
            self._manager.observe(ref, evidence)
        except Exception:  # noqa: BLE001 - one bad PVC must not kill the watch loop
            observer_errors_total.labels(component="pvc_watcher").inc()
            logger.exception("failed handling pvc event", extra={"pvc": pvc.metadata.name})

    def _handle_node_event(self, event_type: EventType, node: V1Node) -> None:
        if event_type == "DELETED" or node.metadata is None:
            return
        try:
            ref = resource_ref_from_node(node)
            evidence = node_evidence(node)
            self._manager.observe(ref, evidence)
        except Exception:  # noqa: BLE001 - one bad node must not kill the watch loop
            observer_errors_total.labels(component="node_watcher").inc()
            logger.exception("failed handling node event", extra={"node": node.metadata.name})

    def _handle_endpointslice_event(self, event_type: EventType, endpointslice: dict) -> None:
        # endpointslice is a plain dict, not a typed V1EndpointSlice -- see
        # observer/watchers.py::_ListWatchLoop's docstring for why.
        if event_type == "DELETED":
            return
        metadata = endpointslice.get("metadata") or {}
        labels = metadata.get("labels") or {}
        service_name = labels.get("kubernetes.io/service-name")
        namespace = metadata.get("namespace")
        if not service_name or not namespace:
            return
        try:
            service = self._core.read_namespaced_service(name=service_name, namespace=namespace)
            if not service.spec or not service.spec.selector:
                # No selector (headless service with manually-managed endpoints,
                # ExternalName, ...) -- "matches no Pods" has no meaning here.
                return
            ref = resource_ref_from_service(service)
            evidence = endpointslice_evidence(endpointslice)
            self._manager.observe(ref, evidence)
        except ApiException as exc:
            if exc.status != 404:  # Service itself already gone -- nothing to diagnose
                observer_errors_total.labels(component="endpointslice_watcher").inc()
                logger.warning("failed resolving service for endpointslice", extra={"status": exc.status})
        except Exception:  # noqa: BLE001 - one bad endpointslice must not kill the watch loop
            observer_errors_total.labels(component="endpointslice_watcher").inc()
            logger.exception(
                "failed handling endpointslice event", extra={"endpointslice": metadata.get("name")}
            )

    def start(self) -> None:
        for name, watcher in self._watchers.items():
            thread = threading.Thread(target=watcher.run, args=(self._stop_event,), daemon=True, name=name)
            thread.start()
            self._threads.append(thread)
        logger.info("observer service started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        logger.info("observer service stopped")

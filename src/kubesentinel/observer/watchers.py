"""Watch loops for Pods, Events, and Deployments, with bounded resync and
reconnect-on-failure.

Mirrors the list-then-watch pattern Kubernetes informers use: do a full LIST to get a
baseline and a resourceVersion, then WATCH from that resourceVersion. If the watch is
invalidated (HTTP 410 Gone -- the resourceVersion aged out of the API server's cache),
relist from scratch. A resync timer also forces a periodic relist even on a healthy
watch stream, as a safety net against any missed or dropped events.

This module intentionally does not poll on a tight interval -- it blocks on the watch
stream and only wakes for stop/resync signals.

The three resource watchers below (Pod, Event, Deployment) are thin wrappers around
one shared `_ListWatchLoop`: the reconnect/resync/backoff logic is identical across
resource types and was duplicated three times before this -- only the specific
list/watch API calls and the callback type differ per resource.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol

from kubernetes import client, watch
from kubernetes.client.rest import ApiException

from kubesentinel.metrics.registry import observer_errors_total
from kubesentinel.observer.backoff import compute_backoff_seconds

logger = logging.getLogger(__name__)

EventType = Literal["ADDED", "MODIFIED", "DELETED"]


class Watcher(Protocol):
    """Shared shape of PodWatcher/EventWatcher/DeploymentWatcher, for callers that
    hold a heterogeneous collection of them (see ObserverService)."""

    def run(self, stop_event: threading.Event) -> None: ...


class _ListWatchLoop:
    """Generic list-then-watch loop, parametrized by the resource's list functions.

    `raw_object_watch=True` works around a real bug in the generated kubernetes
    client: several discovery/v1 models (EndpointSlice's `endpoints` among them)
    mark a list field as required, but the API server legitimately omits it (sends
    JSON `null`) for a genuinely empty list -- Go's `omitempty` on a nil slice
    serializes to `null`, not `[]`. The client's strict model deserializer then
    raises ValueError and kills the entire list/watch call, not just one object.
    Confirmed live against this project's own kind cluster with a zero-endpoint
    EndpointSlice. The fix: fetch raw JSON (`_preload_content=False`) for LIST, and
    pass `watch.Watch(return_type="object")` for WATCH -- both skip the typed model
    deserializer entirely and hand back plain dicts instead.
    """

    def __init__(
        self,
        list_namespaced: Callable[..., Any],
        list_all_namespaces: Callable[..., Any],
        namespaces: list[str],
        on_event: Callable[[EventType, Any], None],
        resync_interval_seconds: int,
        component: str,
        raw_object_watch: bool = False,
        dict_object_watch: bool = False,
    ):
        self._list_namespaced = list_namespaced
        self._list_all_namespaces = list_all_namespaces
        self._namespaces = namespaces
        self._on_event = on_event
        self._resync_interval = resync_interval_seconds
        self._component = component
        self._raw_object_watch = raw_object_watch
        # `CustomObjectsApi` (any CRD -- no generated client has a typed model
        # for one) already returns a plain `dict` from both its list and watch
        # calls, with no `_preload_content=False`/`json.loads` dance needed at
        # all -- confirmed live against a real CRD before writing this, not
        # assumed: `list_namespaced_custom_object()` returns `{"items": [...],
        # "metadata": {"resourceVersion": ...}}` directly, and
        # `watch.Watch()` (the plain, non-`raw_object_watch` constructor)
        # already yields `event["object"]` as a dict too. This is therefore a
        # third, distinct mode from `raw_object_watch` above (which exists to
        # work around a *typed*-model deserialization bug for EndpointSlice),
        # not a variant of it.
        self._dict_object_watch = dict_object_watch

    def _list(self, **kwargs: Any) -> tuple[list[Any], str | None]:
        if self._namespaces:
            # Multiple namespaces require one list call each; merge results.
            items: list[Any] = []
            resource_version = None
            for ns in self._namespaces:
                items_part, resource_version = self._list_one(self._list_namespaced, namespace=ns, **kwargs)
                items.extend(items_part)
            return items, resource_version
        return self._list_one(self._list_all_namespaces, **kwargs)

    def _list_one(self, list_fn: Callable[..., Any], **kwargs: Any) -> tuple[list[Any], str | None]:
        if self._dict_object_watch:
            resp = list_fn(**kwargs)
            return resp.get("items", []), resp.get("metadata", {}).get("resourceVersion")
        if not self._raw_object_watch:
            resp = list_fn(**kwargs)
            return resp.items, resp.metadata.resource_version
        raw = list_fn(_preload_content=False, **kwargs)
        data = json.loads(raw.data)
        return data.get("items", []), data.get("metadata", {}).get("resourceVersion")

    def run(self, stop_event: threading.Event) -> None:
        attempt = 0
        while not stop_event.is_set():
            try:
                items, resource_version = self._list()
                for item in items:
                    self._on_event("ADDED", item)

                attempt = 0  # successful list resets backoff
                self._watch_from(resource_version, stop_event)
            except ApiException as exc:
                if exc.status == 410:
                    logger.info(f"{self._component} watch resourceVersion expired, relisting")
                    continue
                observer_errors_total.labels(component=self._component).inc()
                logger.warning(
                    f"{self._component} watch API error, backing off", extra={"status": exc.status}
                )
            except Exception:  # noqa: BLE001 - must never crash the observer thread
                observer_errors_total.labels(component=self._component).inc()
                logger.exception(f"{self._component} watch failed unexpectedly, backing off")

            delay = compute_backoff_seconds(attempt)
            attempt += 1
            stop_event.wait(delay)

    def _watch_from(self, resource_version: str | None, stop_event: threading.Event) -> None:
        w = watch.Watch(return_type="object") if self._raw_object_watch else watch.Watch()
        resync_deadline = time.monotonic() + self._resync_interval
        kwargs = {"resource_version": resource_version, "timeout_seconds": self._resync_interval}

        stream = (
            w.stream(self._list_all_namespaces, **kwargs)
            if not self._namespaces
            else w.stream(self._list_namespaced, namespace=self._namespaces[0], **kwargs)
        )
        try:
            for event in stream:
                if stop_event.is_set() or time.monotonic() >= resync_deadline:
                    break
                self._on_event(event["type"], event["object"])
        finally:
            w.stop()


class PodWatcher:
    """Watches Pods across the configured namespaces (or all namespaces)."""

    def __init__(
        self,
        core_api: client.CoreV1Api,
        namespaces: list[str],
        on_pod_event: Callable[[EventType, client.V1Pod], None],
        resync_interval_seconds: int = 300,
    ):
        self._loop = _ListWatchLoop(
            list_namespaced=core_api.list_namespaced_pod,
            list_all_namespaces=core_api.list_pod_for_all_namespaces,
            namespaces=namespaces,
            on_event=on_pod_event,
            resync_interval_seconds=resync_interval_seconds,
            component="pod_watcher",
        )

    def run(self, stop_event: threading.Event) -> None:
        self._loop.run(stop_event)


class EventWatcher:
    """Watches core Events (Warning/Normal) across the configured namespaces."""

    def __init__(
        self,
        core_api: client.CoreV1Api,
        namespaces: list[str],
        on_event: Callable[[EventType, client.CoreV1Event], None],
        resync_interval_seconds: int = 300,
    ):
        self._loop = _ListWatchLoop(
            list_namespaced=core_api.list_namespaced_event,
            list_all_namespaces=core_api.list_event_for_all_namespaces,
            namespaces=namespaces,
            on_event=on_event,
            resync_interval_seconds=resync_interval_seconds,
            component="event_watcher",
        )

    def run(self, stop_event: threading.Event) -> None:
        self._loop.run(stop_event)


class NodeWatcher:
    """Watches Nodes, which are cluster-scoped -- never filtered by namespace."""

    def __init__(
        self,
        core_api: client.CoreV1Api,
        on_node_event: Callable[[EventType, client.V1Node], None],
        resync_interval_seconds: int = 300,
    ):
        self._loop = _ListWatchLoop(
            list_namespaced=core_api.list_node,  # never called: namespaces=[] below
            list_all_namespaces=core_api.list_node,
            namespaces=[],
            on_event=on_node_event,
            resync_interval_seconds=resync_interval_seconds,
            component="node_watcher",
        )

    def run(self, stop_event: threading.Event) -> None:
        self._loop.run(stop_event)


class EndpointSliceWatcher:
    """Watches EndpointSlices across the configured namespaces (or all namespaces).

    Delivers plain `dict` objects, not typed `V1EndpointSlice` instances -- see
    `_ListWatchLoop`'s docstring for why (a real bug in the generated client's
    strict model for this resource, confirmed live).
    """

    def __init__(
        self,
        discovery_api: client.DiscoveryV1Api,
        namespaces: list[str],
        on_endpointslice_event: Callable[[EventType, dict], None],
        resync_interval_seconds: int = 300,
    ):
        self._loop = _ListWatchLoop(
            list_namespaced=discovery_api.list_namespaced_endpoint_slice,
            list_all_namespaces=discovery_api.list_endpoint_slice_for_all_namespaces,
            namespaces=namespaces,
            on_event=on_endpointslice_event,
            resync_interval_seconds=resync_interval_seconds,
            component="endpointslice_watcher",
            raw_object_watch=True,
        )

    def run(self, stop_event: threading.Event) -> None:
        self._loop.run(stop_event)


class PVCWatcher:
    """Watches PersistentVolumeClaims across the configured namespaces (or all)."""

    def __init__(
        self,
        core_api: client.CoreV1Api,
        namespaces: list[str],
        on_pvc_event: Callable[[EventType, client.V1PersistentVolumeClaim], None],
        resync_interval_seconds: int = 300,
    ):
        self._loop = _ListWatchLoop(
            list_namespaced=core_api.list_namespaced_persistent_volume_claim,
            list_all_namespaces=core_api.list_persistent_volume_claim_for_all_namespaces,
            namespaces=namespaces,
            on_event=on_pvc_event,
            resync_interval_seconds=resync_interval_seconds,
            component="pvc_watcher",
        )

    def run(self, stop_event: threading.Event) -> None:
        self._loop.run(stop_event)


class DeploymentWatcher:
    """Watches Deployments across the configured namespaces (or all namespaces)."""

    def __init__(
        self,
        apps_api: client.AppsV1Api,
        namespaces: list[str],
        on_deployment_event: Callable[[EventType, client.V1Deployment], None],
        resync_interval_seconds: int = 300,
    ):
        self._loop = _ListWatchLoop(
            list_namespaced=apps_api.list_namespaced_deployment,
            list_all_namespaces=apps_api.list_deployment_for_all_namespaces,
            namespaces=namespaces,
            on_event=on_deployment_event,
            resync_interval_seconds=resync_interval_seconds,
            component="deployment_watcher",
        )

    def run(self, stop_event: threading.Event) -> None:
        self._loop.run(stop_event)


class CustomResourceWatcher:
    """Watches instances of one namespaced CRD across the configured
    namespaces -- added for Milestone 8's operator
    (`KubeSentinelRemediationPolicy`), but generic over `group`/`version`/
    `plural` for any future namespaced CRD. Delivers plain `dict` objects,
    never a typed model -- `CustomObjectsApi` has none for any CRD; see
    `_ListWatchLoop.__init__`'s own comment on `dict_object_watch` for why
    this needed a third mode, not a reuse of `raw_object_watch`.

    Cluster-scoped CRDs are out of scope here (this project's only CRD is
    namespaced, matching `KubeSentinelRemediationPolicy`'s "one CR per
    namespace a human opts in" design -- see docs/OPERATOR_DESIGN.md §4);
    a cluster-scoped variant would need `list_cluster_custom_object` wired
    the way `NodeWatcher` wires its own cluster-scoped list function above.
    """

    def __init__(
        self,
        custom_objects_api: client.CustomObjectsApi,
        group: str,
        version: str,
        plural: str,
        namespaces: list[str],
        on_event: Callable[[EventType, dict], None],
        resync_interval_seconds: int = 300,
    ):
        self._loop = _ListWatchLoop(
            list_namespaced=lambda namespace, **kw: custom_objects_api.list_namespaced_custom_object(
                group=group, version=version, namespace=namespace, plural=plural, **kw
            ),
            list_all_namespaces=lambda **kw: custom_objects_api.list_cluster_custom_object(
                group=group, version=version, plural=plural, **kw
            ),
            namespaces=namespaces,
            on_event=on_event,
            resync_interval_seconds=resync_interval_seconds,
            component="custom_resource_watcher",
            dict_object_watch=True,
        )

    def run(self, stop_event: threading.Event) -> None:
        self._loop.run(stop_event)

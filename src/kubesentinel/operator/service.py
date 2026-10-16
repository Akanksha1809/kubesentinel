"""Wires the CustomResourceWatcher to RemediationPolicyController.reconcile --
the operator's own equivalent of observer/service.py::ObserverService, same
start/stop-threaded-watcher shape.
"""

from __future__ import annotations

import logging
import threading

from kubesentinel.config import Settings
from kubesentinel.metrics.registry import observer_errors_total
from kubesentinel.observer.client import ClusterClients
from kubesentinel.observer.watchers import CustomResourceWatcher, EventType
from kubesentinel.operator import GROUP, PLURAL, VERSION
from kubesentinel.operator.controller import RemediationPolicyController

logger = logging.getLogger(__name__)


class OperatorService:
    def __init__(self, clients: ClusterClients, settings: Settings, controller: RemediationPolicyController):
        self._controller = controller
        self._watcher = CustomResourceWatcher(
            custom_objects_api=clients.custom_objects,
            group=GROUP,
            version=VERSION,
            plural=PLURAL,
            namespaces=settings.watch_namespace_list,
            on_event=self._handle_event,
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _handle_event(self, event_type: EventType, cr: dict) -> None:
        if event_type == "DELETED":
            # Nothing to reconcile for an object that no longer exists --
            # and nothing to clean up either: every action this controller
            # ever approves is recorded permanently on its own incident,
            # the same as an action a human approved would be.
            return
        try:
            self._controller.reconcile(cr)
        except Exception:  # noqa: BLE001 - one bad CR must not kill the operator thread
            observer_errors_total.labels(component="operator").inc()
            logger.exception(
                "failed reconciling RemediationPolicy",
                extra={"policy_name": cr.get("metadata", {}).get("name")},
            )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._watcher.run, args=(self._stop_event,), daemon=True, name="operator")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

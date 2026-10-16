from datetime import datetime, timezone
from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from kubesentinel.observer.service import ObserverService
from tests.unit.factories import (
    make_deployment,
    make_endpointslice,
    make_event,
    make_node,
    make_pod,
    make_pvc,
    make_service,
    running_container_status,
    waiting_container_status,
)


def _service_with_mock_manager() -> tuple[ObserverService, MagicMock]:
    manager = MagicMock()
    # Bypass PodWatcher construction (needs a real CoreV1Api); only the _handle_* methods are under test.
    service = ObserverService.__new__(ObserverService)
    service._manager = manager
    service._core = MagicMock()
    # _still_exists compares the live object's UID against the Event's claimed
    # UID, not just whether a same-named object exists -- default both lookups to
    # return an object whose UID matches make_event's default involved_object_uid,
    # so "still exists" is the default a test has to deliberately override (either
    # with a 404 ApiException, or a mismatched UID) rather than get for free.
    service._core.read_namespaced_pod.return_value.metadata.uid = "uid-1234"
    service._core.read_namespaced_persistent_volume_claim.return_value.metadata.uid = "uid-1234"
    return service, manager


def test_deleted_event_is_ignored():
    service, manager = _service_with_mock_manager()
    pod = make_pod(container_statuses=[waiting_container_status()])
    service._handle_pod_event("DELETED", pod)
    manager.observe.assert_not_called()


def test_terminating_pod_is_not_treated_as_evidence():
    # A pod with deletionTimestamp set may transiently show healthy-looking container
    # statuses while tearing down; this must never be used to resolve an incident.
    service, manager = _service_with_mock_manager()
    pod = make_pod(container_statuses=[running_container_status()])
    pod.metadata.deletion_timestamp = datetime.now(timezone.utc)
    service._handle_pod_event("MODIFIED", pod)
    manager.observe.assert_not_called()


def test_healthy_running_pod_without_deletion_timestamp_is_observed():
    service, manager = _service_with_mock_manager()
    pod = make_pod(container_statuses=[running_container_status()])
    service._handle_pod_event("MODIFIED", pod)
    manager.observe.assert_called_once()


def test_unhealthy_event_is_forwarded_to_manager():
    service, manager = _service_with_mock_manager()
    event = make_event(reason="Unhealthy", message="Readiness probe failed: connection refused")
    service._handle_event("MODIFIED", event)
    manager.observe.assert_called_once()


def test_routine_event_is_filtered_out():
    service, manager = _service_with_mock_manager()
    event = make_event(reason="Scheduled", message="Successfully assigned pod to node")
    service._handle_event("MODIFIED", event)
    manager.observe.assert_not_called()


def test_deleted_event_type_is_ignored_for_events_too():
    service, manager = _service_with_mock_manager()
    event = make_event(reason="Unhealthy", message="Readiness probe failed")
    service._handle_event("DELETED", event)
    manager.observe.assert_not_called()


def test_event_for_non_pod_object_is_ignored():
    service, manager = _service_with_mock_manager()
    event = make_event(
        reason="Unhealthy", message="Readiness probe failed", involved_object_kind="ReplicaSet"
    )
    service._handle_event("MODIFIED", event)
    manager.observe.assert_not_called()


def test_deployment_event_is_forwarded_to_manager():
    service, manager = _service_with_mock_manager()
    deployment = make_deployment(desired_replicas=2, ready_replicas=1)
    service._handle_deployment_event("MODIFIED", deployment)
    manager.observe.assert_called_once()


def test_deleted_deployment_event_is_ignored():
    service, manager = _service_with_mock_manager()
    deployment = make_deployment()
    service._handle_deployment_event("DELETED", deployment)
    manager.observe.assert_not_called()


def test_pvc_binding_failure_event_is_forwarded():
    service, manager = _service_with_mock_manager()
    event = make_event(
        reason="ProvisioningFailed", message="failed", involved_object_kind="PersistentVolumeClaim"
    )
    service._handle_event("MODIFIED", event)
    manager.observe.assert_called_once()


def test_failed_mount_event_is_forwarded():
    # Regression test for a real bug found live: _BAD_EVENT_REASONS (health.py)
    # and _RELEVANT_EVENT_REASONS (this module) are two separate filter lists --
    # adding "FailedMount" to the former without the latter meant the real
    # EventWatcher silently dropped every live FailedMount Event before it ever
    # reached IncidentManager.observe(), so FailedMountRule could never fire
    # outside of hand-built unit-test evidence.
    service, manager = _service_with_mock_manager()
    event = make_event(
        reason="FailedMount", message="MountVolume.SetUp failed", involved_object_kind="Pod"
    )
    service._handle_event("MODIFIED", event)
    manager.observe.assert_called_once()


def test_event_for_already_deleted_pvc_does_not_open_a_phantom_incident():
    # Regression test for a bug found live: Events outlive the resource they
    # describe (separate GC/TTL), so a ProvisioningFailed Event for an
    # already-deleted PVC was still present on process restart and opened an
    # incident for a PVC that no longer existed.
    service, manager = _service_with_mock_manager()
    service._core.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)
    event = make_event(
        reason="ProvisioningFailed", message="failed", involved_object_kind="PersistentVolumeClaim"
    )
    service._handle_event("MODIFIED", event)
    manager.observe.assert_not_called()


def test_event_for_a_deleted_pvc_whose_name_was_reused_does_not_open_a_phantom_incident():
    # Regression test for a bug found live, immediately after fixing the one
    # above: a stale Event about a deleted PVC was "revalidated" because a
    # *different* PVC with the same name had since been created. A name match on
    # an object with a different UID is exactly as stale as no object at all.
    service, manager = _service_with_mock_manager()
    service._core.read_namespaced_persistent_volume_claim.return_value.metadata.uid = "a-completely-different-uid"
    event = make_event(
        reason="ProvisioningFailed",
        message="failed",
        involved_object_kind="PersistentVolumeClaim",
        involved_object_uid="uid-1234",
    )
    service._handle_event("MODIFIED", event)
    manager.observe.assert_not_called()


def test_event_for_still_existing_pod_is_forwarded():
    service, manager = _service_with_mock_manager()
    event = make_event(reason="Unhealthy", message="Readiness probe failed")
    service._handle_event("MODIFIED", event)
    manager.observe.assert_called_once()


def test_event_for_already_deleted_pod_does_not_open_a_phantom_incident():
    service, manager = _service_with_mock_manager()
    service._core.read_namespaced_pod.side_effect = ApiException(status=404)
    event = make_event(reason="Unhealthy", message="Readiness probe failed")
    service._handle_event("MODIFIED", event)
    manager.observe.assert_not_called()


def test_pvc_phase_update_is_forwarded():
    service, manager = _service_with_mock_manager()
    pvc = make_pvc(phase="Bound")
    service._handle_pvc_event("MODIFIED", pvc)
    manager.observe.assert_called_once()


def test_deleted_pvc_event_is_ignored():
    service, manager = _service_with_mock_manager()
    pvc = make_pvc()
    service._handle_pvc_event("DELETED", pvc)
    manager.observe.assert_not_called()


def test_node_event_is_forwarded():
    service, manager = _service_with_mock_manager()
    service._handle_node_event("MODIFIED", make_node())
    manager.observe.assert_called_once()


def test_deleted_node_event_is_ignored():
    service, manager = _service_with_mock_manager()
    service._handle_node_event("DELETED", make_node())
    manager.observe.assert_not_called()


def test_endpointslice_event_resolves_service_and_forwards():
    service, manager = _service_with_mock_manager()
    service._core.read_namespaced_service.return_value = make_service(selector={"app": "test-service"})
    endpointslice = make_endpointslice(endpoint_count=0)
    service._handle_endpointslice_event("MODIFIED", endpointslice)
    manager.observe.assert_called_once()


def test_endpointslice_without_service_label_is_ignored():
    service, manager = _service_with_mock_manager()
    endpointslice = make_endpointslice()
    endpointslice["metadata"]["labels"] = {}
    service._handle_endpointslice_event("MODIFIED", endpointslice)
    manager.observe.assert_not_called()


def test_endpointslice_for_selectorless_service_is_ignored():
    service, manager = _service_with_mock_manager()
    service._core.read_namespaced_service.return_value = make_service(selector=None)
    service._core.read_namespaced_service.return_value.spec.selector = None
    endpointslice = make_endpointslice()
    service._handle_endpointslice_event("MODIFIED", endpointslice)
    manager.observe.assert_not_called()


def test_endpointslice_for_deleted_service_is_ignored():
    service, manager = _service_with_mock_manager()
    service._core.read_namespaced_service.side_effect = ApiException(status=404)
    endpointslice = make_endpointslice()
    service._handle_endpointslice_event("MODIFIED", endpointslice)
    manager.observe.assert_not_called()


def test_deleted_endpointslice_event_is_ignored():
    service, manager = _service_with_mock_manager()
    service._handle_endpointslice_event("DELETED", make_endpointslice())
    manager.observe.assert_not_called()

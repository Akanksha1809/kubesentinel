"""Live kind-cluster scenario for `FailedMountRule` (Milestone 1's own
"stabilize the core" audit, item 2 -- see docs/DIAGNOSTIC_RULES.md). Reads
the real `scenario-failed-mount` Pod's real Events and runs them through
the real `DiagnosticEngine`, never hand-built evidence.
"""

from __future__ import annotations

import pytest

from kubesentinel.diagnostics.engine import DiagnosticEngine
from kubesentinel.incidents.health import has_failure_signal
from kubesentinel.models.diagnosis import ReasonCode
from kubesentinel.observer.inspector import event_evidence, pod_evidence
from tests.conftest import LAB_NAMESPACE
from tests.integration.conftest import poll_until

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")

_SCENARIO_DEPLOYMENT = "failed-mount-demo"


def _scenario_pod(live_cluster_clients):
    pods = live_cluster_clients.core.list_namespaced_pod(
        namespace=LAB_NAMESPACE, label_selector=f"app={_SCENARIO_DEPLOYMENT}"
    ).items
    if not pods:
        pytest.skip(
            f"no Pod found for '{_SCENARIO_DEPLOYMENT}' -- run ./lab/scripts/run-scenario.sh failed-mount first"
        )
    return pods[0]


def test_failed_mount_scenario_is_diagnosed_as_failed_mount(live_cluster_clients):
    pod = _scenario_pod(live_cluster_clients)

    def _real_failed_mount_events():
        events = live_cluster_clients.core.list_namespaced_event(
            namespace=LAB_NAMESPACE,
            field_selector=f"involvedObject.name={pod.metadata.name},reason=FailedMount",
        ).items
        return events or None

    events = poll_until(_real_failed_mount_events, timeout=30)
    if not events:
        pytest.skip(f"no real FailedMount Event observed yet for {pod.metadata.name} -- scenario may still be starting")

    evidence = pod_evidence(pod) + [event_evidence(e) for e in events]
    result = DiagnosticEngine().diagnose(evidence)

    assert result.top_hypothesis is not None
    assert result.top_hypothesis.reason_code == ReasonCode.FAILED_MOUNT
    assert result.top_hypothesis.confidence.value == "high"


def test_failed_mount_event_is_a_real_failure_signal(live_cluster_clients):
    pod = _scenario_pod(live_cluster_clients)

    def _real_failed_mount_events():
        events = live_cluster_clients.core.list_namespaced_event(
            namespace=LAB_NAMESPACE,
            field_selector=f"involvedObject.name={pod.metadata.name},reason=FailedMount",
        ).items
        return events or None

    events = poll_until(_real_failed_mount_events, timeout=30)
    if not events:
        pytest.skip(f"no real FailedMount Event observed yet for {pod.metadata.name} -- scenario may still be starting")

    # Without "FailedMount" in incidents/health.py::_BAD_EVENT_REASONS, a
    # Pod stuck this way would never open an incident at all -- this is
    # the live version of that pin.
    evidence = [event_evidence(e) for e in events]
    assert has_failure_signal(evidence) is True

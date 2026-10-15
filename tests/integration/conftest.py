"""Fixtures and helpers shared across tests/integration/ only."""

from __future__ import annotations

import time

import pytest

from tests.conftest import LAB_NAMESPACE

POLL_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5


def poll_until(predicate, timeout: float = POLL_TIMEOUT_SECONDS, interval: float = POLL_INTERVAL_SECONDS):
    """Polls `predicate()` until it returns a truthy value or `timeout` elapses,
    returning whatever the last call returned (truthy result, or the final
    falsy one on timeout). Used instead of a fixed `sleep` wherever a test is
    waiting on the real observer's watch loop to notice something."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


@pytest.fixture
def probe_failure_pod_name(live_cluster_clients) -> str:
    """The `readiness-probe-failure` scenario's Pod name.

    Deliberately not `crashloop-demo`: found live while writing these tests --
    `crashloop-demo`'s kubelet restart backoff grows exponentially (minutes,
    once the demo has been running a while), so the Pod can sit in a long
    "waiting: CrashLoopBackOff" window with only a brief ~2s healthy-looking
    gap between restarts. A test that happens to start during that gap would
    see no bad signal at all and then wait out the *entire* next backoff
    interval (which can be several minutes) before the next one fires --
    correct behavior, but a bad foundation for a fast, reliably-passing test.
    `readiness-probe-failure-demo`'s failure signal is a recurring Kubernetes
    Event (`periodSeconds=3`, forever, never backing off), so a short poll
    window reliably sees it regardless of exactly when the test happens to
    start relative to the demo's own uptime.
    """
    pods = live_cluster_clients.core.list_namespaced_pod(
        namespace=LAB_NAMESPACE, label_selector="app=readiness-probe-failure-demo"
    )
    if not pods.items:
        pytest.skip(
            "no 'readiness-probe-failure-demo' pod found -- run "
            "./lab/scripts/run-scenario.sh readiness-probe-failure (or ./scripts/demo.sh) first"
        )
    return pods.items[0].metadata.name


@pytest.fixture
def pending_resources_pod_name(live_cluster_clients) -> str:
    """The `pending-resources` scenario's Pod name.

    Used specifically for the delete-and-recreate dedup test, not
    `readiness-probe-failure-demo`: found live while writing these tests --
    deleting a Pod and waiting for its *replacement's* first `Unhealthy` Event
    is itself unreliable in this environment, because kubelet/client-go's
    built-in event-spam filter (a per-process burst+rate-limit on repeated
    identical reason/message events) had, after many hours of this exact
    scenario continuously re-triggering across this project's whole session,
    already exhausted its burst allowance -- the new pod's first probe
    failure event was observed live to take over a minute to actually appear,
    not the ~5s the probe's own `periodSeconds`/`initialDelaySeconds` would
    suggest. `pending-resources-demo`'s failure signal is a Pod *condition*
    (`PodScheduled=False/Unschedulable`), written directly onto the new Pod's
    own status and delivered on the very next Pod watch event -- not routed
    through the separate Event-recording pipeline at all, so it isn't subject
    to that throttling and reliably appears within a couple of seconds of the
    replacement Pod's creation.
    """
    pods = live_cluster_clients.core.list_namespaced_pod(
        namespace=LAB_NAMESPACE, label_selector="app=pending-resources-demo"
    )
    if not pods.items:
        pytest.skip(
            "no 'pending-resources-demo' pod found -- run "
            "./lab/scripts/run-scenario.sh pending-resources (or ./scripts/demo.sh) first"
        )
    return pods.items[0].metadata.name

"""Live check that the bounded resync safety net in `observer/watchers.py`
actually fires against a real API server, not just that the code compiles.

`_ListWatchLoop._watch_from` passes `timeout_seconds=resync_interval_seconds`
to the watch stream itself, so the real Kubernetes API server ends the watch
connection on its own once that elapses (confirmed here, not assumed), and
`run()` then relists from scratch. With a short `resync_interval_seconds`, a
real PodWatcher run for a few multiples of that interval should relist (and
therefore re-emit "ADDED" for every currently-existing Pod) more than once --
proof the reconnect-and-relist path genuinely executes against a live server,
not only the unit-tested backoff math in `test_backoff.py`.

This does not exercise the HTTP 410 Gone path specifically (that requires a
resourceVersion aging out of the API server's cache, which needs far more
churn or wait than is practical in a fast test) -- see TODO.md for that
explicitly scoped-out case.
"""

from __future__ import annotations

import threading
import time

import pytest

from kubesentinel.observer.watchers import EventType, PodWatcher
from tests.conftest import LAB_NAMESPACE

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")


def test_pod_watcher_relists_on_resync_boundary(live_cluster_clients):
    added_event_timestamps: list[float] = []
    lock = threading.Lock()

    def on_pod_event(event_type: EventType, pod) -> None:
        if event_type == "ADDED":
            with lock:
                added_event_timestamps.append(time.monotonic())

    watcher = PodWatcher(
        core_api=live_cluster_clients.core,
        namespaces=[LAB_NAMESPACE],
        on_pod_event=on_pod_event,
        resync_interval_seconds=2,
    )
    stop_event = threading.Event()
    thread = threading.Thread(target=watcher.run, args=(stop_event,), daemon=True)
    thread.start()
    try:
        # A bit over 3 resync intervals: comfortably enough for at least two
        # full list-then-watch cycles even with scheduling jitter.
        time.sleep(7)
    finally:
        stop_event.set()
        thread.join(timeout=5.0)

    with lock:
        timestamps = list(added_event_timestamps)

    assert timestamps, "expected at least one ADDED event from the initial list"
    # Each relist re-emits ADDED for every currently-existing pod in the
    # namespace, so the number of *distinct* list cycles shows up as clusters of
    # near-simultaneous timestamps roughly resync_interval_seconds apart. The
    # simplest robust signal: the first and last ADDED events are separated by
    # more than one resync interval, which can only happen if a relist (not just
    # one continuous watch stream) occurred in between.
    span = timestamps[-1] - timestamps[0]
    assert span > 2.5, (
        f"ADDED events only spanned {span:.1f}s against a 2s resync interval over a 7s run -- "
        "expected at least one relist boundary to have been crossed"
    )

"""Closes the gap docs/DESIGN_DECISIONS.md and TODO.md's own Milestone-1
known-limitations list (#3) named explicitly: `_ListWatchLoop`'s live WATCH
stream used to always follow `self._namespaces[0]`, so namespaces past the
first were only picked up on the next periodic resync, not on every change.
`_list_single`/`_watch_from` are now parametrized by an explicit namespace
(or `None` for cluster-wide), and `run()` spawns one independent
list-then-watch-then-resync loop per configured namespace when there are
several -- these tests pin both the per-namespace routing and the
concurrency (one namespace's own loop can never block another's).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from kubesentinel.observer.watchers import _ListWatchLoop


def _resp(items: list, resource_version: str = "1") -> MagicMock:
    resp = MagicMock()
    resp.items = items
    resp.metadata.resource_version = resource_version
    return resp


def _loop(namespaces: list[str], **kwargs) -> _ListWatchLoop:
    return _ListWatchLoop(
        list_namespaced=kwargs.pop("list_namespaced", MagicMock()),
        list_all_namespaces=kwargs.pop("list_all_namespaces", MagicMock()),
        namespaces=namespaces,
        on_event=kwargs.pop("on_event", MagicMock()),
        resync_interval_seconds=kwargs.pop("resync_interval_seconds", 300),
        component=kwargs.pop("component", "test"),
        **kwargs,
    )


class TestListSingle:
    def test_with_a_namespace_calls_list_namespaced(self):
        list_namespaced = MagicMock(return_value=_resp([{"id": 1}], "10"))
        loop = _loop(["ns-a"], list_namespaced=list_namespaced)

        items, resource_version = loop._list_single("ns-a")

        list_namespaced.assert_called_once_with(namespace="ns-a")
        assert items == [{"id": 1}]
        assert resource_version == "10"

    def test_with_none_calls_list_all_namespaces(self):
        list_all = MagicMock(return_value=_resp([], "5"))
        loop = _loop([], list_all_namespaces=list_all)

        items, resource_version = loop._list_single(None)

        list_all.assert_called_once_with()
        assert items == []
        assert resource_version == "5"


class TestWatchFromUsesTheGivenNamespace:
    """The exact method that previously hardcoded `self._namespaces[0]`."""

    def test_namespaced_stream_targets_the_passed_namespace(self):
        list_namespaced = MagicMock()
        loop = _loop(["ns-a", "ns-b"], list_namespaced=list_namespaced)
        fake_watch = MagicMock()
        fake_watch.stream.return_value = iter([])

        with patch("kubesentinel.observer.watchers.watch.Watch", return_value=fake_watch):
            loop._watch_from("ns-b", "7", threading.Event())

        fake_watch.stream.assert_called_once_with(
            list_namespaced, namespace="ns-b", resource_version="7", timeout_seconds=300
        )
        fake_watch.stop.assert_called_once()

    def test_cluster_scoped_stream_when_namespace_is_none(self):
        list_all = MagicMock()
        loop = _loop([], list_all_namespaces=list_all)
        fake_watch = MagicMock()
        fake_watch.stream.return_value = iter([])

        with patch("kubesentinel.observer.watchers.watch.Watch", return_value=fake_watch):
            loop._watch_from(None, "3", threading.Event())

        fake_watch.stream.assert_called_once_with(list_all, resource_version="3", timeout_seconds=300)

    def test_different_calls_target_different_namespaces(self):
        # The precise regression this closes: previously every call used
        # `self._namespaces[0]` regardless of which namespace it was for.
        list_namespaced = MagicMock()
        loop = _loop(["ns-a", "ns-b"], list_namespaced=list_namespaced)
        fake_watch = MagicMock()
        fake_watch.stream.return_value = iter([])

        with patch("kubesentinel.observer.watchers.watch.Watch", return_value=fake_watch):
            loop._watch_from("ns-a", None, threading.Event())
            loop._watch_from("ns-b", None, threading.Event())

        namespaces_used = [call.kwargs["namespace"] for call in fake_watch.stream.call_args_list]
        assert namespaces_used == ["ns-a", "ns-b"]


class TestRunOneNamespaceEndToEnd:
    def test_routes_list_and_on_event_to_exactly_the_given_namespace(self):
        items_by_ns = {"ns-a": [{"name": "pod-a"}], "ns-b": [{"name": "pod-b"}]}
        stop_event = threading.Event()

        def list_namespaced(namespace: str, **_kw):
            stop_event.set()  # exit the loop after exactly one pass
            return _resp(items_by_ns[namespace], "1")

        events: list[tuple[str, dict]] = []
        loop = _loop(
            ["ns-a", "ns-b"],
            list_namespaced=MagicMock(side_effect=list_namespaced),
            on_event=lambda event_type, obj: events.append((event_type, obj)),
        )
        fake_watch = MagicMock()
        fake_watch.stream.return_value = iter([])

        with patch("kubesentinel.observer.watchers.watch.Watch", return_value=fake_watch):
            loop._run_one_namespace("ns-a", stop_event)
        assert events == [("ADDED", {"name": "pod-a"})]

        events.clear()
        stop_event.clear()
        with patch("kubesentinel.observer.watchers.watch.Watch", return_value=fake_watch):
            loop._run_one_namespace("ns-b", stop_event)
        assert events == [("ADDED", {"name": "pod-b"})]


class TestRunDispatch:
    def test_zero_namespaces_runs_directly_with_none_no_extra_thread(self):
        loop = _loop([])
        calls = []
        loop._run_one_namespace = lambda namespace, stop_event: calls.append(
            (namespace, threading.current_thread() is threading.main_thread())
        )

        loop.run(threading.Event())

        assert calls == [(None, True)]

    def test_one_namespace_runs_directly_no_extra_thread(self):
        loop = _loop(["ns-a"])
        calls = []
        loop._run_one_namespace = lambda namespace, stop_event: calls.append(
            (namespace, threading.current_thread() is threading.main_thread())
        )

        loop.run(threading.Event())

        assert calls == [("ns-a", True)]

    def test_several_namespaces_each_get_their_own_thread_and_all_are_joined(self):
        loop = _loop(["ns-a", "ns-b", "ns-c"])
        seen: dict[str, threading.Thread] = {}
        lock = threading.Lock()

        def fake_run_one(namespace, stop_event):
            with lock:
                seen[namespace] = threading.current_thread()

        loop._run_one_namespace = fake_run_one
        loop.run(threading.Event())  # must block until every thread finishes

        assert set(seen) == {"ns-a", "ns-b", "ns-c"}
        calling_thread = threading.current_thread()
        threads_used = set()
        for namespace, thread in seen.items():
            assert thread is not calling_thread
            threads_used.add(thread)
        assert len(threads_used) == 3  # each namespace ran on its own, distinct thread

    def test_one_namespace_blocking_never_blocks_another(self):
        # The exact bug this closes: a single watch stream could only ever
        # follow one namespace, so a slow/stuck namespace effectively
        # starved every other configured namespace of live updates.
        loop = _loop(["ns-a", "ns-b"])
        started = set()
        release = threading.Event()
        lock = threading.Lock()

        def fake_run_one(namespace, stop_event):
            with lock:
                started.add(namespace)
            if namespace == "ns-a":
                release.wait(timeout=5)

        loop._run_one_namespace = fake_run_one
        run_thread = threading.Thread(target=loop.run, args=(threading.Event(),))
        run_thread.start()
        try:
            for _ in range(200):
                with lock:
                    if "ns-b" in started:
                        break
                time.sleep(0.01)
            with lock:
                assert "ns-b" in started, "ns-b never started while ns-a was still blocked"
            assert run_thread.is_alive(), "run() returned before ns-a's blocking thread finished"
        finally:
            release.set()
            run_thread.join(timeout=5)
        assert not run_thread.is_alive()

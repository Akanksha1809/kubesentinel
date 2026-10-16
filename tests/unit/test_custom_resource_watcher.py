"""Unit test for the `dict_object_watch` mode `_ListWatchLoop` gained for
Milestone 8's `CustomResourceWatcher` -- see
observer/watchers.py::_ListWatchLoop.__init__'s own comment for why this
needed a third mode, distinct from `raw_object_watch` (confirmed live
against a real CRD before writing either the mode or this test, not
assumed -- see docs/OPERATOR_DESIGN.md §6).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kubesentinel.observer.watchers import CustomResourceWatcher, _ListWatchLoop


def _dict_response(items: list[dict], resource_version: str = "100") -> dict:
    return {"items": items, "metadata": {"resourceVersion": resource_version}}


class TestListWatchLoopDictMode:
    def test_list_one_extracts_items_and_resource_version_from_a_plain_dict(self):
        list_fn = MagicMock(return_value=_dict_response([{"kind": "Foo"}], resource_version="42"))
        loop = _ListWatchLoop(
            list_namespaced=list_fn,
            list_all_namespaces=list_fn,
            namespaces=["kubesentinel-lab"],
            on_event=MagicMock(),
            resync_interval_seconds=300,
            component="test",
            dict_object_watch=True,
        )
        items, resource_version = loop._list_one(list_fn, namespace="kubesentinel-lab")
        assert items == [{"kind": "Foo"}]
        assert resource_version == "42"

    def test_list_one_handles_an_empty_items_list(self):
        list_fn = MagicMock(return_value=_dict_response([]))
        loop = _ListWatchLoop(
            list_namespaced=list_fn,
            list_all_namespaces=list_fn,
            namespaces=[],
            on_event=MagicMock(),
            resync_interval_seconds=300,
            component="test",
            dict_object_watch=True,
        )
        items, resource_version = loop._list_one(list_fn)
        assert items == []
        assert resource_version == "100"


class TestCustomResourceWatcher:
    def test_list_calls_list_namespaced_custom_object_with_the_right_coordinates(self):
        custom_objects_api = MagicMock()
        custom_objects_api.list_namespaced_custom_object.return_value = _dict_response([])
        watcher = CustomResourceWatcher(
            custom_objects_api=custom_objects_api,
            group="kubesentinel.io",
            version="v1alpha1",
            plural="kubesentinelremediationpolicies",
            namespaces=["kubesentinel-lab"],
            on_event=MagicMock(),
        )
        watcher._loop._list()
        custom_objects_api.list_namespaced_custom_object.assert_called_once_with(
            group="kubesentinel.io",
            version="v1alpha1",
            namespace="kubesentinel-lab",
            plural="kubesentinelremediationpolicies",
        )

    def test_list_delivers_each_item_through_on_event_as_added(self):
        # Exercises the same path `run()` does for its initial replay
        # (`_list()` then `on_event("ADDED", item)` per item), without
        # going through `run()`'s own watch half at all -- that half is a
        # real network stream and is covered for every other watcher by
        # `tests/integration/test_watch_resync.py` (live, not unit); this
        # test is specifically pinning the new dict-shaped `_list()` ->
        # `on_event` wiring `dict_object_watch` added.
        custom_objects_api = MagicMock()
        custom_objects_api.list_namespaced_custom_object.return_value = _dict_response(
            [{"kind": "KubeSentinelRemediationPolicy", "metadata": {"name": "p1"}}]
        )
        events = []
        watcher = CustomResourceWatcher(
            custom_objects_api=custom_objects_api,
            group="kubesentinel.io",
            version="v1alpha1",
            plural="kubesentinelremediationpolicies",
            namespaces=["kubesentinel-lab"],
            on_event=lambda event_type, obj: events.append((event_type, obj)),
        )
        items, _resource_version = watcher._loop._list()
        for item in items:
            watcher._loop._on_event("ADDED", item)
        assert events == [("ADDED", {"kind": "KubeSentinelRemediationPolicy", "metadata": {"name": "p1"}})]

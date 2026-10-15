import json
import urllib.error
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from kubesentinel.optimization.metrics_providers import (
    InMemorySampleStore,
    MetricsApiProvider,
    MetricsProviderError,
    PrometheusProvider,
    RawUsageSample,
)


def _pod_metrics_response(items):
    return {"kind": "PodMetricsList", "items": items}


def _pod_metrics_item(name, namespace, containers, timestamp="2026-01-01T00:00:00Z"):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "timestamp": timestamp,
        "containers": [{"name": cname, "usage": {"cpu": cpu, "memory": mem}} for cname, cpu, mem in containers],
    }


class TestMetricsApiProvider:
    def test_get_current_pod_usage_parses_response(self):
        custom_objects = MagicMock()
        custom_objects.list_namespaced_custom_object.return_value = _pod_metrics_response(
            [_pod_metrics_item("demo-pod", "kubesentinel-lab", [("app", "100m", "128Mi")])]
        )
        provider = MetricsApiProvider(custom_objects)

        samples = provider.get_current_pod_usage(namespace="kubesentinel-lab")

        assert len(samples) == 1
        sample = samples[0]
        assert sample.pod_name == "demo-pod"
        assert sample.namespace == "kubesentinel-lab"
        assert sample.container_name == "app"
        assert sample.cpu_millicores == 100
        assert sample.memory_bytes == 128 * 1024 * 1024
        custom_objects.list_namespaced_custom_object.assert_called_once()
        custom_objects.list_cluster_custom_object.assert_not_called()

    def test_get_current_pod_usage_without_namespace_uses_cluster_scoped_call(self):
        custom_objects = MagicMock()
        custom_objects.list_cluster_custom_object.return_value = _pod_metrics_response([])
        provider = MetricsApiProvider(custom_objects)

        provider.get_current_pod_usage(namespace=None)

        custom_objects.list_cluster_custom_object.assert_called_once()
        custom_objects.list_namespaced_custom_object.assert_not_called()

    def test_metrics_server_not_installed_raises_metrics_provider_error(self):
        custom_objects = MagicMock()
        custom_objects.list_namespaced_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        provider = MetricsApiProvider(custom_objects)

        with pytest.raises(MetricsProviderError):
            provider.get_current_pod_usage(namespace="kubesentinel-lab")

    def test_permission_denied_raises_metrics_provider_error(self):
        custom_objects = MagicMock()
        custom_objects.list_namespaced_custom_object.side_effect = ApiException(status=403, reason="Forbidden")
        provider = MetricsApiProvider(custom_objects)

        with pytest.raises(MetricsProviderError):
            provider.get_current_pod_usage(namespace="kubesentinel-lab")

    def test_one_malformed_container_does_not_discard_the_rest(self):
        custom_objects = MagicMock()
        custom_objects.list_namespaced_custom_object.return_value = _pod_metrics_response(
            [
                _pod_metrics_item("bad-pod", "kubesentinel-lab", [("app", "not-a-quantity", "128Mi")]),
                _pod_metrics_item("good-pod", "kubesentinel-lab", [("app", "50m", "64Mi")]),
            ]
        )
        provider = MetricsApiProvider(custom_objects)

        samples = provider.get_current_pod_usage(namespace="kubesentinel-lab")

        assert len(samples) == 1
        assert samples[0].pod_name == "good-pod"

    def test_get_current_node_usage_parses_response(self):
        custom_objects = MagicMock()
        custom_objects.list_cluster_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "kubesentinel-control-plane"},
                    "timestamp": "2026-01-01T00:00:00Z",
                    "usage": {"cpu": "500m", "memory": "1Gi"},
                }
            ]
        }
        provider = MetricsApiProvider(custom_objects)

        samples = provider.get_current_node_usage()

        assert len(samples) == 1
        assert samples[0].node_name == "kubesentinel-control-plane"
        assert samples[0].cpu_millicores == 500
        assert samples[0].memory_bytes == 1024**3

    def test_node_usage_error_raises_metrics_provider_error(self):
        custom_objects = MagicMock()
        custom_objects.list_cluster_custom_object.side_effect = ApiException(status=500, reason="Internal Error")
        provider = MetricsApiProvider(custom_objects)

        with pytest.raises(MetricsProviderError):
            provider.get_current_node_usage()


def _http_response(body: dict) -> BytesIO:
    buf = BytesIO(json.dumps(body).encode("utf-8"))
    buf.__enter__ = lambda self: self
    buf.__exit__ = lambda self, *a: None
    return buf


class TestPrometheusProvider:
    def test_query_range_parses_single_series(self):
        provider = PrometheusProvider("http://localhost:9090")
        body = {
            "status": "success",
            "data": {"result": [{"metric": {}, "values": [[1700000000, "0.5"], [1700000015, "0.6"]]}]},
        }
        with patch("urllib.request.urlopen", return_value=_http_response(body)):
            result = provider.query_range(
                "rate(container_cpu_usage_seconds_total[5m])",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
                15,
            )
        assert len(result) == 2
        assert result[0][1] == 0.5
        assert result[1][1] == 0.6

    def test_query_range_returns_empty_list_for_no_data(self):
        provider = PrometheusProvider("http://localhost:9090")
        body = {"status": "success", "data": {"result": []}}
        with patch("urllib.request.urlopen", return_value=_http_response(body)):
            result = provider.query_range(
                "nonexistent_metric",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
                15,
            )
        assert result == []

    def test_query_range_raises_on_ambiguous_multi_series_result(self):
        provider = PrometheusProvider("http://localhost:9090")
        body = {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"pod": "a"}, "values": [[1700000000, "0.1"]]},
                    {"metric": {"pod": "b"}, "values": [[1700000000, "0.2"]]},
                ]
            },
        }
        with patch("urllib.request.urlopen", return_value=_http_response(body)):
            with pytest.raises(MetricsProviderError):
                provider.query_range(
                    "ambiguous", datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 1, tzinfo=timezone.utc), 15
                )

    def test_query_range_raises_on_prometheus_error_status(self):
        provider = PrometheusProvider("http://localhost:9090")
        body = {"status": "error", "error": "bad query"}
        with patch("urllib.request.urlopen", return_value=_http_response(body)):
            with pytest.raises(MetricsProviderError):
                provider.query_range(
                    "bad", datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 1, tzinfo=timezone.utc), 15
                )

    def test_query_range_raises_on_unreachable_server(self):
        provider = PrometheusProvider("http://localhost:9090")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with pytest.raises(MetricsProviderError):
                provider.query_range(
                    "x", datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 1, tzinfo=timezone.utc), 15
                )


class TestInMemorySampleStore:
    def _sample(self, cpu=100) -> RawUsageSample:
        return RawUsageSample(
            namespace="kubesentinel-lab",
            pod_name="demo-pod",
            container_name="app",
            cpu_millicores=cpu,
            memory_bytes=1024,
            timestamp=datetime.now(timezone.utc),
        )

    def test_add_and_get_roundtrip(self):
        store = InMemorySampleStore(max_samples_per_key=10)
        store.add("pod-uid-1", self._sample(cpu=100))
        store.add("pod-uid-1", self._sample(cpu=200))

        samples = store.get("kubesentinel-lab", "pod-uid-1", "app")

        assert [s.cpu_millicores for s in samples] == [100, 200]

    def test_get_for_unknown_key_returns_empty_list(self):
        store = InMemorySampleStore()
        assert store.get("kubesentinel-lab", "no-such-pod", "app") == []

    def test_bounded_by_max_samples_per_key(self):
        store = InMemorySampleStore(max_samples_per_key=3)
        for i in range(10):
            store.add("pod-uid-1", self._sample(cpu=i))

        samples = store.get("kubesentinel-lab", "pod-uid-1", "app")

        assert len(samples) == 3
        assert [s.cpu_millicores for s in samples] == [7, 8, 9]  # oldest evicted first

    def test_different_containers_are_independent_keys(self):
        store = InMemorySampleStore()
        store.add("pod-uid-1", self._sample(cpu=1))
        sample_b = RawUsageSample(
            namespace="kubesentinel-lab",
            pod_name="demo-pod",
            container_name="sidecar",
            cpu_millicores=2,
            memory_bytes=1024,
            timestamp=datetime.now(timezone.utc),
        )
        store.add("pod-uid-1", sample_b)

        assert len(store.get("kubesentinel-lab", "pod-uid-1", "app")) == 1
        assert len(store.get("kubesentinel-lab", "pod-uid-1", "sidecar")) == 1
        assert len(store) == 2

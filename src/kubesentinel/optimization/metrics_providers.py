"""Metrics providers for the optimization engine.

Two real backends, one `Protocol` (same shape as `ai/client.py::AiClient` and
`observer/watchers.py::Watcher` -- this project's established way of letting
a module depend on "something that can answer X" rather than a concrete
class):

- `MetricsApiProvider`: the Kubernetes Metrics API (`metrics.k8s.io/v1beta1`,
  as served by metrics-server). A *current snapshot* only -- it has no
  history. Queried via `CustomObjectsApi`, since the `kubernetes` client
  (30.1.0) has no typed client for this aggregated API -- confirmed by
  reading the installed package, not assumed.
- `PrometheusProvider`: optional. A real HTTP range query against a
  Prometheus server's standard `/api/v1/query_range`, using cAdvisor's
  conventional `container_cpu_usage_seconds_total`/
  `container_memory_working_set_bytes` metric names (the de facto standard
  across kube-prometheus-stack and similar setups) -- stdlib `urllib` only,
  the same approach as `ai/client.py::OllamaClient`, no new HTTP dependency.

Neither provider ever fabricates a value: a query that fails, times out, or
returns no data raises `MetricsProviderError` (caught by the caller, which
degrades `EvidenceQuality` rather than crashing) or returns an empty list --
never a zero substituted for "unknown."
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from kubernetes.client import CustomObjectsApi
from kubernetes.client.rest import ApiException

from kubesentinel.optimization.quantities import (
    QuantityParseError,
    parse_cpu_to_millicores,
    parse_memory_to_bytes,
)

logger = logging.getLogger(__name__)


class MetricsProviderError(Exception):
    """Raised when a provider cannot answer at all -- unreachable, timed out,
    permission denied, or the aggregated API isn't installed. Distinct from
    "reachable but genuinely no data yet," which returns an empty list."""


@dataclass(frozen=True)
class RawUsageSample:
    namespace: str
    pod_name: str
    container_name: str
    cpu_millicores: int
    memory_bytes: int
    timestamp: datetime


@dataclass(frozen=True)
class NodeUsageSample:
    node_name: str
    cpu_millicores: int
    memory_bytes: int
    timestamp: datetime


class MetricsProvider(Protocol):
    name: str

    def get_current_pod_usage(self, namespace: str | None = None) -> list[RawUsageSample]: ...

    def get_current_node_usage(self) -> list[NodeUsageSample]: ...


def _parse_rfc3339(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    # Python 3.11+'s datetime.fromisoformat natively accepts the trailing "Z"
    # the Kubernetes API always uses -- no manual replacement needed.
    return datetime.fromisoformat(value)


class MetricsApiProvider:
    """Queries `metrics.k8s.io/v1beta1` via `CustomObjectsApi`. A current
    snapshot only: `EvidenceQuality` built on this provider alone never rises
    above `SNAPSHOT_ONLY`/`LIMITED_HISTORY` (the latter only once samples are
    accumulated over time by `InMemorySampleStore`) -- this class itself has
    no memory of past calls.
    """

    name = "metrics_api"

    def __init__(self, custom_objects: CustomObjectsApi, timeout_seconds: float = 10.0):
        self._custom_objects = custom_objects
        self._timeout = timeout_seconds

    def get_current_pod_usage(self, namespace: str | None = None) -> list[RawUsageSample]:
        try:
            if namespace:
                response = self._custom_objects.list_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="pods",
                    _request_timeout=self._timeout,
                )
            else:
                response = self._custom_objects.list_cluster_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="pods",
                    _request_timeout=self._timeout,
                )
        except ApiException as exc:
            # metrics-server not installed at all shows up as 404 on this
            # aggregated API path; a real permission problem is 403. Both are
            # "this provider cannot answer," never silently empty.
            raise MetricsProviderError(
                f"metrics.k8s.io pods query failed (status={exc.status}): {exc.reason}"
            ) from exc

        samples: list[RawUsageSample] = []
        for item in response.get("items", []):
            metadata = item.get("metadata", {})
            pod_name = metadata.get("name", "")
            pod_namespace = metadata.get("namespace", "")
            timestamp = _parse_rfc3339(item.get("timestamp"))
            for container in item.get("containers", []):
                usage = container.get("usage", {})
                try:
                    cpu_millicores = parse_cpu_to_millicores(usage["cpu"])
                    memory_bytes = parse_memory_to_bytes(usage["memory"])
                except (KeyError, QuantityParseError):
                    # One malformed container entry must not discard every
                    # other real sample in the same response -- same
                    # per-object isolation principle as the observer's own
                    # per-event try/except (see observer/service.py).
                    logger.warning(
                        "skipping unparseable PodMetrics container entry",
                        extra={"pod": pod_name, "namespace": pod_namespace},
                    )
                    continue
                samples.append(
                    RawUsageSample(
                        namespace=pod_namespace,
                        pod_name=pod_name,
                        container_name=container.get("name", ""),
                        cpu_millicores=cpu_millicores,
                        memory_bytes=memory_bytes,
                        timestamp=timestamp,
                    )
                )
        return samples

    def get_current_node_usage(self) -> list[NodeUsageSample]:
        try:
            response = self._custom_objects.list_cluster_custom_object(
                group="metrics.k8s.io", version="v1beta1", plural="nodes", _request_timeout=self._timeout
            )
        except ApiException as exc:
            raise MetricsProviderError(
                f"metrics.k8s.io nodes query failed (status={exc.status}): {exc.reason}"
            ) from exc

        samples: list[NodeUsageSample] = []
        for item in response.get("items", []):
            node_name = item.get("metadata", {}).get("name", "")
            usage = item.get("usage", {})
            try:
                cpu_millicores = parse_cpu_to_millicores(usage["cpu"])
                memory_bytes = parse_memory_to_bytes(usage["memory"])
            except (KeyError, QuantityParseError):
                logger.warning("skipping unparseable NodeMetrics entry", extra={"node": node_name})
                continue
            samples.append(
                NodeUsageSample(
                    node_name=node_name,
                    cpu_millicores=cpu_millicores,
                    memory_bytes=memory_bytes,
                    timestamp=_parse_rfc3339(item.get("timestamp")),
                )
            )
        return samples


class PrometheusProvider:
    """Optional real historical-metrics backend. Queries a Prometheus
    server's own HTTP API directly (`urllib`, no client library) using
    cAdvisor's conventional container metric names. Not wired into
    `inventory.py`/`analyzer.py` yet (Milestone 3+); built and unit-tested
    now so the `MetricsProvider`-adjacent range-query capability exists
    behind a stable interface from the start.
    """

    name = "prometheus"

    def __init__(self, base_url: str, timeout_seconds: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def query_range(
        self, promql: str, start: datetime, end: datetime, step_seconds: int
    ) -> list[tuple[datetime, float]]:
        """Returns `(timestamp, value)` pairs for a single-series PromQL
        range query. Raises `MetricsProviderError` if Prometheus is
        unreachable, returns an error status, or the query matches more than
        one series (ambiguous -- never silently picks one)."""
        params = {
            "query": promql,
            "start": f"{start.timestamp():.3f}",
            "end": f"{end.timestamp():.3f}",
            "step": str(step_seconds),
        }
        url = f"{self._base_url}/api/v1/query_range?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.URLError as exc:
            raise MetricsProviderError(f"could not reach Prometheus at {self._base_url}: {exc}") from exc
        except TimeoutError as exc:
            raise MetricsProviderError(f"Prometheus query timed out after {self._timeout}s") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MetricsProviderError(f"Prometheus returned non-JSON response: {raw[:200]!r}") from exc

        if parsed.get("status") != "success":
            raise MetricsProviderError(f"Prometheus query failed: {parsed.get('error', parsed)}")

        result = parsed.get("data", {}).get("result", [])
        if not result:
            return []
        if len(result) > 1:
            raise MetricsProviderError(
                f"PromQL query matched {len(result)} series, expected exactly one: {promql!r}"
            )
        values = result[0].get("values", [])
        return [(datetime.fromtimestamp(float(ts), tz=timezone.utc), float(val)) for ts, val in values]


class InMemorySampleStore:
    """Bounded, process-lifetime-only ring buffer of `RawUsageSample`s per
    `(namespace, pod_uid, container_name)` key -- this project's "poor man's
    history" when only metrics-server (a snapshot-only API) is available.
    Deliberately not a time-series database and not persisted: Prometheus is
    the real answer for anyone who needs retained history (see
    RESOURCE_OPTIMIZATION_DESIGN.md §11); this is just enough memory to tell
    "one sample" apart from "a few samples over a few minutes."
    """

    def __init__(self, max_samples_per_key: int = 60):
        self._max_samples = max_samples_per_key
        self._store: dict[tuple[str, str, str], deque[RawUsageSample]] = {}

    def add(self, pod_uid: str, sample: RawUsageSample) -> None:
        key = (sample.namespace, pod_uid, sample.container_name)
        if key not in self._store:
            self._store[key] = deque(maxlen=self._max_samples)
        self._store[key].append(sample)

    def get(self, namespace: str, pod_uid: str, container_name: str) -> list[RawUsageSample]:
        return list(self._store.get((namespace, pod_uid, container_name), ()))

    def __len__(self) -> int:
        return len(self._store)

"""Live check of `optimization/inventory.py` against the real kind cluster --
not mocked. Confirms both that the inventory it builds matches real,
known-broken/known-healthy lab workloads, and -- at the actual HTTP
transport layer, the strongest form of this guarantee -- that building it
never issues anything but a GET.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kubesentinel.models.optimization import WorkloadKind
from kubesentinel.optimization.inventory import build_workload_inventory
from tests.conftest import LAB_NAMESPACE, REMEDIATION_TEST_DEPLOYMENT

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")


def test_inventory_matches_known_lab_workloads(live_cluster_clients):
    snapshots = build_workload_inventory(live_cluster_clients, namespaces=[LAB_NAMESPACE])
    by_name = {s.workload_name: s for s in snapshots}

    assert REMEDIATION_TEST_DEPLOYMENT in by_name
    remediation_test = by_name[REMEDIATION_TEST_DEPLOYMENT]
    assert remediation_test.workload_kind == WorkloadKind.DEPLOYMENT
    assert remediation_test.healthy is True
    assert remediation_test.desired_replicas == 2
    assert remediation_test.ready_replicas == 2

    # At least one genuinely-broken lab scenario must appear and be flagged
    # unhealthy -- if every workload in the namespace read as healthy,
    # something in the inventory/health logic would be silently wrong.
    unhealthy = [s for s in snapshots if not s.healthy]
    assert unhealthy, "expected at least one unhealthy workload among the real lab scenarios"


def test_inventory_never_issues_a_non_get_http_request(live_cluster_clients):
    pool_manager = live_cluster_clients.core.api_client.rest_client.pool_manager
    original_request = pool_manager.request
    methods_seen: set[str] = set()

    def spy(method, url, *args, **kwargs):
        methods_seen.add(method)
        return original_request(method, url, *args, **kwargs)

    with patch.object(pool_manager, "request", side_effect=spy):
        build_workload_inventory(live_cluster_clients, namespaces=[LAB_NAMESPACE])

    assert methods_seen, "expected at least one HTTP call to have been made"
    assert methods_seen <= {"GET"}, f"inventory build issued non-GET HTTP method(s): {methods_seen}"

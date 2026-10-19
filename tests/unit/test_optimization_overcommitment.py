from datetime import datetime, timezone

from kubesentinel.config import Settings
from kubesentinel.models.optimization import ResourceQuantity
from kubesentinel.models.scheduling import NodeSnapshot
from kubesentinel.optimization.metrics_providers import NodeUsageSample
from kubesentinel.optimization.overcommitment import compute_cluster_overcommitment


def _node(**overrides) -> NodeSnapshot:
    defaults = dict(
        name="node-1",
        allocatable=ResourceQuantity(cpu_millicores=2000, memory_bytes=4 * 1024**3),
        requested=ResourceQuantity(cpu_millicores=0, memory_bytes=0),
        limits=ResourceQuantity(cpu_millicores=0, memory_bytes=0),
    )
    defaults.update(overrides)
    return NodeSnapshot(**defaults)


class TestComputeClusterOvercommitment:
    def test_no_nodes_returns_none(self):
        assert compute_cluster_overcommitment([], Settings()) is None

    def test_requested_and_limits_ratios_computed_per_node(self):
        node = _node(
            allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000),
            requested=ResourceQuantity(cpu_millicores=500, memory_bytes=250),
            limits=ResourceQuantity(cpu_millicores=800, memory_bytes=900),
        )

        result = compute_cluster_overcommitment([node], Settings())

        assert result is not None
        row = result.nodes[0]
        assert row.requested_cpu_ratio == 0.5
        assert row.requested_memory_ratio == 0.25
        assert row.limits_cpu_ratio == 0.8
        assert row.limits_memory_ratio == 0.9

    def test_limits_over_threshold_flags_overcommitted(self):
        node = _node(
            allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000),
            requested=ResourceQuantity(cpu_millicores=500, memory_bytes=500),
            limits=ResourceQuantity(cpu_millicores=1500, memory_bytes=400),
        )

        result = compute_cluster_overcommitment([node], Settings(optimization_overcommitment_limits_ratio_threshold=1.0))

        assert result.nodes[0].overcommitted_cpu is True
        assert result.nodes[0].overcommitted_memory is False
        assert result.overcommitted_cpu is True
        assert result.overcommitted_memory is False

    def test_limits_at_or_below_threshold_not_flagged(self):
        node = _node(
            allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000),
            limits=ResourceQuantity(cpu_millicores=1000, memory_bytes=900),
        )

        result = compute_cluster_overcommitment([node], Settings(optimization_overcommitment_limits_ratio_threshold=1.0))

        assert result.nodes[0].overcommitted_cpu is False

    def test_missing_allocatable_denominator_yields_none_ratio_not_crash(self):
        node = _node(allocatable=ResourceQuantity(cpu_millicores=None, memory_bytes=1000))

        result = compute_cluster_overcommitment([node], Settings())

        assert result.nodes[0].requested_cpu_ratio is None
        assert result.nodes[0].limits_cpu_ratio is None
        assert result.nodes[0].overcommitted_cpu is False

    def test_unlimited_container_counts_carried_through_not_treated_as_zero(self):
        node = _node(unlimited_cpu_container_count=3, unlimited_memory_container_count=1)

        result = compute_cluster_overcommitment([node], Settings())

        assert result.nodes[0].unlimited_cpu_container_count == 3
        assert result.nodes[0].unlimited_memory_container_count == 1

    def test_cluster_wide_totals_aggregate_across_nodes(self):
        node_a = _node(
            name="node-a",
            allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000),
            requested=ResourceQuantity(cpu_millicores=400, memory_bytes=300),
            limits=ResourceQuantity(cpu_millicores=600, memory_bytes=500),
        )
        node_b = _node(
            name="node-b",
            allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000),
            requested=ResourceQuantity(cpu_millicores=100, memory_bytes=200),
            limits=ResourceQuantity(cpu_millicores=1600, memory_bytes=400),
        )

        result = compute_cluster_overcommitment(
            [node_a, node_b], Settings(optimization_overcommitment_limits_ratio_threshold=1.0)
        )

        # allocatable 2000 total, requested 500 total, limits 2200 total
        assert result.cluster_requested_cpu_ratio == 0.25
        assert result.cluster_limits_cpu_ratio == 1.1
        assert result.overcommitted_cpu is True

    def test_one_node_with_unknown_allocatable_makes_cluster_ratio_unknown_not_partial(self):
        node_a = _node(name="node-a", allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000))
        node_b = _node(name="node-b", allocatable=ResourceQuantity(cpu_millicores=None, memory_bytes=1000))

        result = compute_cluster_overcommitment([node_a, node_b], Settings())

        # One Node's allocatable is unknown -- the cluster-wide ratio must
        # not silently sum only the known Nodes and misreport a number
        # that looks complete but excludes real capacity.
        assert result.cluster_requested_cpu_ratio is None
        assert result.cluster_limits_cpu_ratio is None

    def test_cordoned_or_not_ready_node_still_included(self):
        node = _node(unschedulable=True, ready=False)

        result = compute_cluster_overcommitment([node], Settings())

        assert len(result.nodes) == 1

    def test_no_node_usage_leaves_actual_fields_none(self):
        node = _node(allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000))

        result = compute_cluster_overcommitment([node], Settings())

        row = result.nodes[0]
        assert row.actual_cpu_millicores is None
        assert row.actual_memory_bytes is None
        assert row.actual_cpu_ratio is None
        assert row.under_pressure_cpu is False
        assert result.cluster_actual_cpu_ratio is None

    def test_matched_node_usage_computes_actual_ratio(self):
        node = _node(
            name="node-1", allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000)
        )
        usage = [
            NodeUsageSample(
                node_name="node-1", cpu_millicores=600, memory_bytes=400, timestamp=datetime.now(timezone.utc)
            )
        ]

        result = compute_cluster_overcommitment([node], Settings(), node_usage=usage)

        row = result.nodes[0]
        assert row.actual_cpu_millicores == 600
        assert row.actual_cpu_ratio == 0.6
        assert row.actual_memory_ratio == 0.4

    def test_actual_usage_over_pressure_threshold_flags_under_pressure(self):
        node = _node(name="node-1", allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000))
        usage = [
            NodeUsageSample(
                node_name="node-1", cpu_millicores=950, memory_bytes=100, timestamp=datetime.now(timezone.utc)
            )
        ]

        result = compute_cluster_overcommitment(
            [node], Settings(optimization_node_pressure_ratio_threshold=0.9), node_usage=usage
        )

        assert result.nodes[0].under_pressure_cpu is True
        assert result.nodes[0].under_pressure_memory is False
        assert result.under_pressure_cpu is True
        assert result.under_pressure_memory is False

    def test_usage_sample_for_different_node_name_is_not_matched(self):
        node = _node(name="node-1", allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000))
        usage = [
            NodeUsageSample(
                node_name="node-2", cpu_millicores=600, memory_bytes=400, timestamp=datetime.now(timezone.utc)
            )
        ]

        result = compute_cluster_overcommitment([node], Settings(), node_usage=usage)

        assert result.nodes[0].actual_cpu_millicores is None

    def test_one_node_missing_usage_makes_cluster_actual_ratio_unknown_not_partial(self):
        node_a = _node(name="node-a", allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000))
        node_b = _node(name="node-b", allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1000))
        usage = [
            NodeUsageSample(
                node_name="node-a", cpu_millicores=500, memory_bytes=500, timestamp=datetime.now(timezone.utc)
            )
        ]

        result = compute_cluster_overcommitment([node_a, node_b], Settings(), node_usage=usage)

        assert result.cluster_actual_cpu_ratio is None

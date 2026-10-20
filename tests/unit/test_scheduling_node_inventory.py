from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client import V1Container, V1PodSpec, V1PodStatus, V1ResourceRequirements, V1Taint

from tests.unit.factories import make_node, make_pod, node_condition
from kubesentinel.scheduling.node_inventory import (
    build_node_inventory,
    build_pod_label_inventory,
    effective_pod_resource_quantity,
)


def _items(objs):
    return SimpleNamespace(items=objs)


def _clients(nodes=(), pods=()):
    clients = MagicMock()
    clients.core.list_node.return_value = _items(list(nodes))
    clients.core.list_pod_for_all_namespaces.return_value = _items(list(pods))
    return clients


def _pod_with_request(name, node_name, cpu="100m", memory="128Mi", phase="Running"):
    pod = make_pod(name=name, phase=phase)
    pod.spec = V1PodSpec(
        node_name=node_name,
        containers=[V1Container(name="app", resources=V1ResourceRequirements(requests={"cpu": cpu, "memory": memory}))],
    )
    pod.status = V1PodStatus(phase=phase)
    return pod


def _pod_with_limits(name, node_name, cpu=None, memory=None, phase="Running"):
    limits = {}
    if cpu is not None:
        limits["cpu"] = cpu
    if memory is not None:
        limits["memory"] = memory
    pod = make_pod(name=name, phase=phase)
    pod.spec = V1PodSpec(
        node_name=node_name,
        containers=[V1Container(name="app", resources=V1ResourceRequirements(limits=limits or None))],
    )
    pod.status = V1PodStatus(phase=phase)
    return pod


class TestBuildNodeInventory:
    def test_single_node_no_pods(self):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        clients = _clients(nodes=[node], pods=[])

        snapshots = build_node_inventory(clients)

        assert len(snapshots) == 1
        assert snapshots[0].name == "node-1"
        assert snapshots[0].allocatable.cpu_millicores == 2000
        assert snapshots[0].requested.cpu_millicores == 0

    def test_sums_requests_from_pods_bound_to_the_node(self):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        pod_a = _pod_with_request("a", "node-1", cpu="500m", memory="256Mi")
        pod_b = _pod_with_request("b", "node-1", cpu="300m", memory="128Mi")
        clients = _clients(nodes=[node], pods=[pod_a, pod_b])

        snapshots = build_node_inventory(clients)

        assert snapshots[0].requested.cpu_millicores == 800

    def test_pods_on_a_different_node_are_not_counted(self):
        node = make_node(name="node-1", allocatable={"cpu": "2"})
        other_node_pod = _pod_with_request("a", "node-2", cpu="500m")
        clients = _clients(nodes=[node], pods=[other_node_pod])

        snapshots = build_node_inventory(clients)

        assert snapshots[0].requested.cpu_millicores == 0

    def test_unbound_pod_not_counted(self):
        node = make_node(name="node-1", allocatable={"cpu": "2"})
        pod = make_pod(name="pending-pod", phase="Pending")
        pod.spec = V1PodSpec(node_name=None, containers=[V1Container(name="app")])
        clients = _clients(nodes=[node], pods=[pod])

        snapshots = build_node_inventory(clients)

        assert snapshots[0].requested.cpu_millicores == 0

    def test_terminal_phase_pods_not_counted(self):
        node = make_node(name="node-1", allocatable={"cpu": "2"})
        succeeded = _pod_with_request("done", "node-1", cpu="500m", phase="Succeeded")
        clients = _clients(nodes=[node], pods=[succeeded])

        snapshots = build_node_inventory(clients)

        assert snapshots[0].requested.cpu_millicores == 0

    def test_taints_are_extracted(self):
        node = make_node(name="node-1", taints=[V1Taint(key="dedicated", value="gpu", effect="NoSchedule")])
        clients = _clients(nodes=[node], pods=[])

        snapshots = build_node_inventory(clients)

        assert len(snapshots[0].taints) == 1
        assert snapshots[0].taints[0].key == "dedicated"
        assert snapshots[0].taints[0].effect == "NoSchedule"

    def test_labels_are_extracted(self):
        node = make_node(name="node-1", labels={"disktype": "ssd"})
        clients = _clients(nodes=[node], pods=[])

        snapshots = build_node_inventory(clients)

        assert snapshots[0].labels == {"disktype": "ssd"}

    def test_unschedulable_flag_extracted(self):
        node = make_node(name="node-1", unschedulable=True)
        clients = _clients(nodes=[node], pods=[])

        snapshots = build_node_inventory(clients)

        assert snapshots[0].unschedulable is True

    def test_not_ready_node_reads_as_not_ready(self):
        node = make_node(name="node-1", conditions=[node_condition("Ready", "False")])
        clients = _clients(nodes=[node], pods=[])

        snapshots = build_node_inventory(clients)

        assert snapshots[0].ready is False

    def test_multiple_nodes_each_get_their_own_requests(self):
        node1 = make_node(name="node-1", allocatable={"cpu": "2"})
        node2 = make_node(name="node-2", allocatable={"cpu": "2"})
        pod1 = _pod_with_request("a", "node-1", cpu="500m")
        pod2 = _pod_with_request("b", "node-2", cpu="300m")
        clients = _clients(nodes=[node1, node2], pods=[pod1, pod2])

        snapshots = build_node_inventory(clients)
        by_name = {s.name: s for s in snapshots}

        assert by_name["node-1"].requested.cpu_millicores == 500
        assert by_name["node-2"].requested.cpu_millicores == 300

    def test_sums_limits_from_pods_bound_to_the_node(self):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        pod_a = _pod_with_limits("a", "node-1", cpu="500m", memory="256Mi")
        pod_b = _pod_with_limits("b", "node-1", cpu="300m", memory="128Mi")
        clients = _clients(nodes=[node], pods=[pod_a, pod_b])

        snapshots = build_node_inventory(clients)

        assert snapshots[0].limits.cpu_millicores == 800
        assert snapshots[0].limits.memory_bytes == (256 + 128) * 1024 * 1024

    def test_container_with_no_limits_tracked_as_unlimited_count_not_hidden(self):
        # The node-level `limits` sum is 0 here, same as `requested` would
        # be for a pod with no requests either -- the real signal that this
        # 0 understates the actual worst case lives in the separate
        # unlimited-count fields, never folded into the sum itself.
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        unlimited = _pod_with_limits("a", "node-1")
        clients = _clients(nodes=[node], pods=[unlimited])

        snapshots = build_node_inventory(clients)

        assert snapshots[0].limits.cpu_millicores == 0
        assert snapshots[0].limits.memory_bytes == 0
        assert snapshots[0].unlimited_cpu_container_count == 1
        assert snapshots[0].unlimited_memory_container_count == 1

    def test_container_with_only_cpu_limit_counts_memory_as_unlimited(self):
        node = make_node(name="node-1", allocatable={"cpu": "2", "memory": "4Gi"})
        pod = _pod_with_limits("a", "node-1", cpu="500m")
        clients = _clients(nodes=[node], pods=[pod])

        snapshots = build_node_inventory(clients)

        assert snapshots[0].limits.cpu_millicores == 500
        assert snapshots[0].limits.memory_bytes == 0
        assert snapshots[0].unlimited_cpu_container_count == 0
        assert snapshots[0].unlimited_memory_container_count == 1


class TestBuildPodLabelInventory:
    def test_extracts_namespace_node_and_labels_for_bound_pods(self):
        pod = _pod_with_request("a", "node-1")
        pod.metadata.labels = {"app": "demo"}
        clients = _clients(nodes=[], pods=[pod])

        summaries = build_pod_label_inventory(clients)

        assert len(summaries) == 1
        assert summaries[0].uid == pod.metadata.uid
        assert summaries[0].namespace == pod.metadata.namespace
        assert summaries[0].node_name == "node-1"
        assert summaries[0].labels == {"app": "demo"}

    def test_unbound_pod_excluded(self):
        pod = make_pod(name="pending-pod", phase="Pending")
        pod.spec = V1PodSpec(node_name=None, containers=[V1Container(name="app")])
        clients = _clients(nodes=[], pods=[pod])

        assert build_pod_label_inventory(clients) == []

    def test_terminal_phase_pod_excluded(self):
        pod = _pod_with_request("done", "node-1", phase="Succeeded")
        clients = _clients(nodes=[], pods=[pod])

        assert build_pod_label_inventory(clients) == []

    def test_missing_labels_defaults_to_empty_dict(self):
        pod = _pod_with_request("a", "node-1")
        pod.metadata.labels = None
        clients = _clients(nodes=[], pods=[pod])

        summaries = build_pod_label_inventory(clients)

        assert summaries[0].labels == {}


def _container(name="app", requests=None, limits=None, restart_policy=None):
    return V1Container(
        name=name,
        resources=V1ResourceRequirements(requests=requests, limits=limits),
        restart_policy=restart_policy,
    )


class TestEffectivePodResourceQuantity:
    def test_no_init_containers_sums_regular_containers(self):
        result = effective_pod_resource_quantity(
            [_container(requests={"cpu": "100m"}), _container(requests={"cpu": "200m"})],
            None,
            "requests",
            context="test",
        )
        assert result.cpu_millicores == 300

    def test_init_container_smaller_than_regular_sum_is_ignored(self):
        # Real scheduler semantics: init containers run one at a time,
        # never concurrently with each other or the regular containers,
        # so a small init container never raises the effective request
        # above what the regular containers alone already need.
        result = effective_pod_resource_quantity(
            [_container(requests={"cpu": "300m"})],
            [_container(name="init", requests={"cpu": "100m"})],
            "requests",
            context="test",
        )
        assert result.cpu_millicores == 300

    def test_init_container_larger_than_regular_sum_wins(self):
        # The real gap this function closes: a single large init
        # container's own request can exceed the regular containers'
        # sum, and the *effective* Pod request must reflect that larger
        # number, not silently understate it.
        result = effective_pod_resource_quantity(
            [_container(requests={"cpu": "100m"})],
            [_container(name="init", requests={"cpu": "500m"})],
            "requests",
            context="test",
        )
        assert result.cpu_millicores == 500

    def test_multiple_init_containers_take_the_largest_single_one_not_a_sum(self):
        # Init containers run sequentially, never concurrently with each
        # other -- two 300m init containers never need 600m at once,
        # only the larger of the two, ever.
        result = effective_pod_resource_quantity(
            [],
            [_container(name="init-a", requests={"cpu": "300m"}), _container(name="init-b", requests={"cpu": "200m"})],
            "requests",
            context="test",
        )
        assert result.cpu_millicores == 300

    def test_limits_field_uses_the_same_algorithm_as_requests(self):
        result = effective_pod_resource_quantity(
            [_container(limits={"memory": "128Mi"})],
            [_container(name="init", limits={"memory": "512Mi"})],
            "limits",
            context="test",
        )
        assert result.memory_bytes == 512 * 1024 * 1024

    def test_no_containers_at_all_is_none_not_zero(self):
        result = effective_pod_resource_quantity([], [], "requests", context="test")
        assert result.cpu_millicores is None
        assert result.memory_bytes is None

    def test_only_init_containers_no_regular_ones(self):
        result = effective_pod_resource_quantity(
            [], [_container(name="init", requests={"cpu": "250m"})], "requests", context="test"
        )
        assert result.cpu_millicores == 250

    def test_cpu_and_memory_evaluated_independently(self):
        # A large init-container CPU request must not leak into the
        # memory comparison, and vice versa.
        result = effective_pod_resource_quantity(
            [_container(requests={"cpu": "100m", "memory": "256Mi"})],
            [_container(name="init", requests={"cpu": "500m"})],
            "requests",
            context="test",
        )
        assert result.cpu_millicores == 500
        assert result.memory_bytes == 256 * 1024 * 1024


class TestEffectivePodResourceQuantityWithNativeSidecars:
    """`restartPolicy: Always` init containers ("native sidecars,"
    Kubernetes 1.28+) keep running once started, concurrently with every
    later init container and every regular container -- closing the gap
    docs/SCHEDULING_ANALYSIS.md Section 13.3 named explicitly."""

    def test_sidecar_adds_into_the_regular_sum_once_running(self):
        # The exact gap this closes: a sidecar smaller than the regular
        # sum must still add to it in the final steady state, not just
        # compete against it the way a plain init container would.
        result = effective_pod_resource_quantity(
            [_container(requests={"cpu": "100m"})],
            [_container(name="sidecar", requests={"cpu": "300m"}, restart_policy="Always")],
            "requests",
            context="test",
        )
        assert result.cpu_millicores == 400

    def test_matches_the_kubernetes_docs_own_worked_example(self):
        # init-1 (sidecar, 2 CPU) starts and keeps running; init-2 (plain,
        # 1 CPU) starts next, alongside init-1; container-1 (2 CPU) is the
        # eventual steady state, alongside init-1. Effective = max(2, 2+1,
        # 2+2) = 4 -- pinned against the authoritative example at
        # https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/#resource-sharing-within-containers.
        result = effective_pod_resource_quantity(
            [_container(name="container-1", requests={"cpu": "2"})],
            [
                _container(name="init-1", requests={"cpu": "2"}, restart_policy="Always"),
                _container(name="init-2", requests={"cpu": "1"}),
            ],
            "requests",
            context="test",
        )
        assert result.cpu_millicores == 4000

    def test_a_later_plain_init_container_does_not_sum_with_an_earlier_one(self):
        # Two plain init containers still never sum with each other --
        # only a *sidecar* accumulates. A sidecar before a plain init
        # container adds to that container's own candidate; two plain
        # ones never add to each other's.
        result = effective_pod_resource_quantity(
            [],
            [
                _container(name="sidecar", requests={"cpu": "100m"}, restart_policy="Always"),
                _container(name="plain-a", requests={"cpu": "300m"}),
                _container(name="plain-b", requests={"cpu": "50m"}),
            ],
            "requests",
            context="test",
        )
        # plain-a's candidate: 100 (sidecar) + 300 = 400. plain-b's: 100 + 50 = 150.
        # Never 100 + 300 + 50 = 450 -- plain-a and plain-b never add to each other.
        assert result.cpu_millicores == 400

    def test_declaration_order_determines_which_init_containers_see_the_sidecar(self):
        # A sidecar only counts toward an init container's own candidate
        # if it was declared *before* that container -- it hasn't started
        # yet if declared after.
        sidecar_first = effective_pod_resource_quantity(
            [_container(requests={"cpu": "10m"})],
            [
                _container(name="sidecar", requests={"cpu": "100m"}, restart_policy="Always"),
                _container(name="plain", requests={"cpu": "50m"}),
            ],
            "requests",
            context="test",
        )
        plain_first = effective_pod_resource_quantity(
            [_container(requests={"cpu": "10m"})],
            [
                _container(name="plain", requests={"cpu": "50m"}),
                _container(name="sidecar", requests={"cpu": "100m"}, restart_policy="Always"),
            ],
            "requests",
            context="test",
        )
        # sidecar first: plain's own candidate is 100 (sidecar) + 50 = 150,
        # the overall max.
        assert sidecar_first.cpu_millicores == 150
        # plain first: plain's own candidate is only 50 (the sidecar hasn't
        # started yet) -- the steady state (10 regular + 100 sidecar = 110)
        # wins instead. Same two containers, same final sidecar
        # contribution, different init-phase peak purely from order.
        assert plain_first.cpu_millicores == 110

    def test_multiple_sidecars_all_accumulate_in_the_steady_state(self):
        result = effective_pod_resource_quantity(
            [_container(requests={"cpu": "10m"})],
            [
                _container(name="sidecar-a", requests={"cpu": "100m"}, restart_policy="Always"),
                _container(name="sidecar-b", requests={"cpu": "50m"}, restart_policy="Always"),
            ],
            "requests",
            context="test",
        )
        assert result.cpu_millicores == 160  # 10 + 100 + 50


class TestBuildNodeInventoryWithInitContainers:
    def test_large_init_container_raises_the_pods_contribution_to_requested(self):
        node = make_node(name="node-1", allocatable={"cpu": "2"})
        pod = make_pod(name="a", phase="Running")
        pod.spec = V1PodSpec(
            node_name="node-1",
            containers=[V1Container(name="app", resources=V1ResourceRequirements(requests={"cpu": "50m"}))],
            init_containers=[
                V1Container(name="init", resources=V1ResourceRequirements(requests={"cpu": "500m"}))
            ],
        )
        pod.status = V1PodStatus(phase="Running")
        clients = _clients(nodes=[node], pods=[pod])

        snapshots = build_node_inventory(clients)

        # Before this fix, init containers were ignored entirely and this
        # would have read 50 (the regular container's own request only).
        assert snapshots[0].requested.cpu_millicores == 500

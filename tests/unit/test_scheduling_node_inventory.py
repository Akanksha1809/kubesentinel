from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client import V1Container, V1PodSpec, V1PodStatus, V1ResourceRequirements, V1Taint

from tests.unit.factories import make_node, make_pod, node_condition
from kubesentinel.scheduling.node_inventory import build_node_inventory, build_pod_label_inventory


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

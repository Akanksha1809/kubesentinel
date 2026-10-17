from kubernetes.client import V1Container, V1PodSpec, V1ResourceRequirements, V1Toleration

from kubesentinel.models.optimization import ResourceQuantity
from kubesentinel.models.scheduling import ConstraintType, NodeSnapshot
from kubesentinel.scheduling.analyzer import (
    _detect_unevaluated_constraints,
    analyze_pod_scheduling,
    deserialize_pod_spec,
)


def _node(**overrides) -> NodeSnapshot:
    defaults = dict(
        name="node-1",
        allocatable=ResourceQuantity(cpu_millicores=2000, memory_bytes=4 * 1024**3),
        requested=ResourceQuantity(cpu_millicores=0, memory_bytes=0),
    )
    defaults.update(overrides)
    return NodeSnapshot(**defaults)


def _pod_spec(cpu="100m", memory="128Mi", **overrides) -> V1PodSpec:
    defaults = dict(containers=[V1Container(name="app", resources=V1ResourceRequirements(requests={"cpu": cpu, "memory": memory}))])
    defaults.update(overrides)
    return V1PodSpec(**defaults)


class TestDeterminism:
    def test_same_input_produces_same_result(self):
        node = _node()
        spec = _pod_spec()
        f1 = analyze_pod_scheduling(spec, [node], namespace="ns")
        f2 = analyze_pod_scheduling(spec, [node], namespace="ns")
        assert f1.any_node_schedulable == f2.any_node_schedulable
        assert f1.node_results[0].schedulable == f2.node_results[0].schedulable
        assert [r.reasoning for r in f1.node_results[0].constraint_results] == [
            r.reasoning for r in f2.node_results[0].constraint_results
        ]


class TestFindingShape:
    def test_schedulable_on_a_fitting_node(self):
        node = _node()
        finding = analyze_pod_scheduling(_pod_spec(), [node], namespace="ns", pod_name="p")
        assert finding.any_node_schedulable is True
        assert finding.node_results[0].schedulable is True
        assert "node-1" in finding.reasoning

    def test_unschedulable_when_no_node_fits(self):
        node = _node(allocatable=ResourceQuantity(cpu_millicores=10, memory_bytes=10))
        finding = analyze_pod_scheduling(_pod_spec(), [node], namespace="ns")
        assert finding.any_node_schedulable is False
        assert finding.node_results[0].schedulable is False
        assert "Not schedulable" in finding.reasoning

    def test_cordoned_node_is_never_schedulable_even_if_resources_fit(self):
        node = _node(unschedulable=True)
        finding = analyze_pod_scheduling(_pod_spec(), [node], namespace="ns")
        assert finding.node_results[0].schedulable is False
        assert finding.node_results[0].constraint_results == []

    def test_not_ready_node_is_never_schedulable(self):
        node = _node(ready=False)
        finding = analyze_pod_scheduling(_pod_spec(), [node], namespace="ns")
        assert finding.node_results[0].schedulable is False

    def test_evaluated_constraints_exclude_quota_by_default(self):
        node = _node()
        finding = analyze_pod_scheduling(_pod_spec(), [node], namespace="ns")
        assert ConstraintType.NAMESPACE_QUOTA not in finding.evaluated_constraints
        assert ConstraintType.RESOURCE_FIT in finding.evaluated_constraints

    def test_check_quota_true_includes_quota_in_evaluated(self):
        node = _node()
        finding = analyze_pod_scheduling(_pod_spec(), [node], namespace="ns", check_quota=True, quotas=[])
        assert ConstraintType.NAMESPACE_QUOTA in finding.evaluated_constraints

    def test_multiple_nodes_schedulable_if_any_one_fits(self):
        good = _node(name="good")
        bad = _node(name="bad", allocatable=ResourceQuantity(cpu_millicores=1, memory_bytes=1))
        finding = analyze_pod_scheduling(_pod_spec(), [bad, good], namespace="ns")
        assert finding.any_node_schedulable is True
        by_name = {r.node_name: r.schedulable for r in finding.node_results}
        assert by_name == {"bad": False, "good": True}

    def test_scheduler_message_is_carried_through(self):
        node = _node()
        finding = analyze_pod_scheduling(
            _pod_spec(), [node], namespace="ns", scheduler_message="0/1 nodes are available: 1 Insufficient cpu."
        )
        assert finding.scheduler_message == "0/1 nodes are available: 1 Insufficient cpu."

    def test_workload_fields_carried_through(self):
        node = _node()
        finding = analyze_pod_scheduling(
            _pod_spec(), [node], namespace="ns", pod_name="p", workload_kind="Deployment", workload_name="my-app"
        )
        assert finding.workload_kind == "Deployment"
        assert finding.workload_name == "my-app"


class TestUnevaluatedConstraintDetection:
    def test_no_affinity_or_topology_spread_detects_nothing(self):
        spec = _pod_spec()
        assert _detect_unevaluated_constraints(spec) == []

    def test_preferred_node_affinity_detected(self):
        from kubernetes.client import V1Affinity, V1NodeAffinity, V1NodeSelectorTerm, V1PreferredSchedulingTerm

        spec = _pod_spec(
            affinity=V1Affinity(
                node_affinity=V1NodeAffinity(
                    preferred_during_scheduling_ignored_during_execution=[
                        V1PreferredSchedulingTerm(weight=1, preference=V1NodeSelectorTerm())
                    ]
                )
            )
        )
        assert ConstraintType.PREFERRED_NODE_AFFINITY in _detect_unevaluated_constraints(spec)

    def test_required_pod_affinity_with_evaluable_term_is_not_unevaluated(self):
        """A required podAffinity term using only `topologyKey`/
        `labelSelector` is now actually evaluated (REQUIRED_POD_AFFINITY in
        `evaluated_constraints`), so it must NOT also show up here --
        `check_pod_affinity` already covers it, never both lists at once."""
        from kubernetes.client import V1Affinity, V1LabelSelector, V1PodAffinity, V1PodAffinityTerm

        spec = _pod_spec(
            affinity=V1Affinity(
                pod_affinity=V1PodAffinity(
                    required_during_scheduling_ignored_during_execution=[
                        V1PodAffinityTerm(topology_key="zone", label_selector=V1LabelSelector())
                    ]
                )
            )
        )
        assert ConstraintType.POD_AFFINITY not in _detect_unevaluated_constraints(spec)

    def test_preferred_pod_affinity_detected(self):
        from kubernetes.client import V1Affinity, V1PodAffinity, V1PodAffinityTerm, V1WeightedPodAffinityTerm

        spec = _pod_spec(
            affinity=V1Affinity(
                pod_affinity=V1PodAffinity(
                    preferred_during_scheduling_ignored_during_execution=[
                        V1WeightedPodAffinityTerm(weight=1, pod_affinity_term=V1PodAffinityTerm(topology_key="zone"))
                    ]
                )
            )
        )
        assert ConstraintType.POD_AFFINITY in _detect_unevaluated_constraints(spec)

    def test_required_pod_affinity_with_namespace_selector_term_detected(self):
        """A required term using `namespaceSelector` can't be evaluated by
        `check_pod_affinity` either -- still surfaced here so a caller
        knows the required-side result may be incomplete."""
        from kubernetes.client import V1Affinity, V1LabelSelector, V1PodAffinity, V1PodAffinityTerm

        spec = _pod_spec(
            affinity=V1Affinity(
                pod_affinity=V1PodAffinity(
                    required_during_scheduling_ignored_during_execution=[
                        V1PodAffinityTerm(topology_key="zone", namespace_selector=V1LabelSelector())
                    ]
                )
            )
        )
        assert ConstraintType.POD_AFFINITY in _detect_unevaluated_constraints(spec)

    def test_required_pod_anti_affinity_with_evaluable_term_is_not_unevaluated(self):
        from kubernetes.client import V1Affinity, V1LabelSelector, V1PodAffinityTerm, V1PodAntiAffinity

        spec = _pod_spec(
            affinity=V1Affinity(
                pod_anti_affinity=V1PodAntiAffinity(
                    required_during_scheduling_ignored_during_execution=[
                        V1PodAffinityTerm(topology_key="zone", label_selector=V1LabelSelector())
                    ]
                )
            )
        )
        assert ConstraintType.POD_ANTI_AFFINITY not in _detect_unevaluated_constraints(spec)

    def test_preferred_pod_anti_affinity_detected(self):
        from kubernetes.client import V1Affinity, V1PodAffinityTerm, V1PodAntiAffinity, V1WeightedPodAffinityTerm

        spec = _pod_spec(
            affinity=V1Affinity(
                pod_anti_affinity=V1PodAntiAffinity(
                    preferred_during_scheduling_ignored_during_execution=[
                        V1WeightedPodAffinityTerm(weight=1, pod_affinity_term=V1PodAffinityTerm(topology_key="zone"))
                    ]
                )
            )
        )
        assert ConstraintType.POD_ANTI_AFFINITY in _detect_unevaluated_constraints(spec)

    def test_required_pod_anti_affinity_with_namespace_selector_term_detected(self):
        from kubernetes.client import V1Affinity, V1LabelSelector, V1PodAffinityTerm, V1PodAntiAffinity

        spec = _pod_spec(
            affinity=V1Affinity(
                pod_anti_affinity=V1PodAntiAffinity(
                    required_during_scheduling_ignored_during_execution=[
                        V1PodAffinityTerm(topology_key="zone", namespace_selector=V1LabelSelector())
                    ]
                )
            )
        )
        assert ConstraintType.POD_ANTI_AFFINITY in _detect_unevaluated_constraints(spec)

    def test_topology_spread_constraint_is_no_longer_reported_as_unevaluated(self):
        """Every topologySpreadConstraint is now evaluated for real
        (TOPOLOGY_SPREAD moved to `EVALUATED_CONSTRAINT_TYPES`), so
        `_detect_unevaluated_constraints` must never report it."""
        from kubernetes.client import V1LabelSelector, V1TopologySpreadConstraint

        spec = _pod_spec(
            topology_spread_constraints=[
                V1TopologySpreadConstraint(max_skew=1, topology_key="zone", when_unsatisfiable="DoNotSchedule", label_selector=V1LabelSelector())
            ]
        )
        assert ConstraintType.TOPOLOGY_SPREAD not in _detect_unevaluated_constraints(spec)

    def test_topology_spread_constraint_appears_as_evaluated_in_the_finding(self):
        from kubernetes.client import V1LabelSelector, V1TopologySpreadConstraint

        node = _node()
        spec = _pod_spec(
            topology_spread_constraints=[
                V1TopologySpreadConstraint(max_skew=1, topology_key="zone", when_unsatisfiable="DoNotSchedule", label_selector=V1LabelSelector())
            ]
        )
        finding = analyze_pod_scheduling(spec, [node], namespace="ns")
        assert ConstraintType.TOPOLOGY_SPREAD in finding.evaluated_constraints
        assert ConstraintType.TOPOLOGY_SPREAD not in finding.unevaluated_constraints


class TestHypotheticalSpec:
    def test_deserializes_a_raw_dict_into_a_usable_spec(self):
        raw = {
            "containers": [{"name": "app", "image": "busybox", "resources": {"requests": {"cpu": "200m", "memory": "64Mi"}}}],
            "nodeSelector": {"disktype": "ssd"},
            "tolerations": [{"key": "foo", "operator": "Exists", "effect": "NoSchedule"}],
        }
        spec = deserialize_pod_spec(raw)
        assert spec.node_selector == {"disktype": "ssd"}
        assert isinstance(spec.tolerations[0], V1Toleration)
        assert spec.containers[0].resources.requests == {"cpu": "200m", "memory": "64Mi"}

    def test_hypothetical_spec_can_be_analyzed_like_a_real_one(self):
        node = _node()
        raw = {"containers": [{"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}
        spec = deserialize_pod_spec(raw)
        finding = analyze_pod_scheduling(spec, [node], namespace="ns")
        assert finding.pod_name is None
        assert finding.any_node_schedulable is True

    def test_quota_blocks_hypothetical_even_when_nodes_fit(self):
        from kubernetes.client import V1ObjectMeta, V1ResourceQuota, V1ResourceQuotaStatus

        node = _node()
        quota = V1ResourceQuota(
            metadata=V1ObjectMeta(name="tight-quota"),
            status=V1ResourceQuotaStatus(hard={"requests.cpu": "50m"}, used={"requests.cpu": "0"}),
        )
        finding = analyze_pod_scheduling(_pod_spec(cpu="100m"), [node], namespace="ns", check_quota=True, quotas=[quota])
        assert finding.any_node_schedulable is False

from kubernetes.client import (
    V1Affinity,
    V1LabelSelector,
    V1LabelSelectorRequirement,
    V1NodeAffinity,
    V1NodeSelector,
    V1NodeSelectorRequirement,
    V1NodeSelectorTerm,
    V1ObjectMeta,
    V1PodAffinity,
    V1PodAffinityTerm,
    V1PodAntiAffinity,
    V1ResourceQuota,
    V1ResourceQuotaStatus,
    V1Toleration,
    V1TopologySpreadConstraint,
)

from kubesentinel.models.optimization import ResourceQuantity
from kubesentinel.models.scheduling import ConstraintType, NodeSnapshot, NodeTaint, PodLabelSummary
from kubesentinel.scheduling.constraints import (
    check_namespace_quota,
    check_node_selector,
    check_pod_affinity,
    check_pod_anti_affinity,
    check_required_node_affinity,
    check_resource_fit,
    check_taints_tolerations,
    check_topology_spread_constraints,
    label_selector_matches,
)


def _node(**overrides) -> NodeSnapshot:
    defaults = dict(
        name="node-1",
        allocatable=ResourceQuantity(cpu_millicores=2000, memory_bytes=4 * 1024**3),
        requested=ResourceQuantity(cpu_millicores=0, memory_bytes=0),
    )
    defaults.update(overrides)
    return NodeSnapshot(**defaults)


class TestResourceFit:
    def test_fits_when_well_under_allocatable(self):
        node = _node(requested=ResourceQuantity(cpu_millicores=500, memory_bytes=1024**3))
        result = check_resource_fit(ResourceQuantity(cpu_millicores=500, memory_bytes=1024**3), node)
        assert result.constraint_type == ConstraintType.RESOURCE_FIT
        assert result.satisfied is True

    def test_fits_exactly_at_the_boundary(self):
        node = _node(allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=1024), requested=ResourceQuantity(cpu_millicores=0, memory_bytes=0))
        result = check_resource_fit(ResourceQuantity(cpu_millicores=1000, memory_bytes=1024), node)
        assert result.satisfied is True

    def test_insufficient_cpu(self):
        node = _node(allocatable=ResourceQuantity(cpu_millicores=1000, memory_bytes=4 * 1024**3), requested=ResourceQuantity(cpu_millicores=800, memory_bytes=0))
        result = check_resource_fit(ResourceQuantity(cpu_millicores=500, memory_bytes=0), node)
        assert result.satisfied is False
        assert "cpu" in result.reasoning
        assert "short by 300m" in result.reasoning

    def test_insufficient_memory(self):
        node = _node(allocatable=ResourceQuantity(cpu_millicores=2000, memory_bytes=1000), requested=ResourceQuantity(cpu_millicores=0, memory_bytes=900))
        result = check_resource_fit(ResourceQuantity(cpu_millicores=0, memory_bytes=200), node)
        assert result.satisfied is False
        assert "memory" in result.reasoning

    def test_insufficient_both_reports_both(self):
        node = _node(allocatable=ResourceQuantity(cpu_millicores=100, memory_bytes=100), requested=ResourceQuantity(cpu_millicores=0, memory_bytes=0))
        result = check_resource_fit(ResourceQuantity(cpu_millicores=200, memory_bytes=200), node)
        assert result.satisfied is False
        assert "cpu" in result.reasoning and "memory" in result.reasoning

    def test_unknown_allocatable_is_not_a_failure(self):
        node = _node(allocatable=ResourceQuantity(cpu_millicores=None, memory_bytes=None))
        result = check_resource_fit(ResourceQuantity(cpu_millicores=10_000, memory_bytes=10_000), node)
        assert result.satisfied is True


class TestTaintsTolerations:
    def test_no_taints_is_satisfied(self):
        node = _node(taints=[])
        result = check_taints_tolerations([], node)
        assert result.satisfied is True

    def test_untolerated_no_schedule_taint_blocks(self):
        node = _node(taints=[NodeTaint(key="dedicated", value="gpu", effect="NoSchedule")])
        result = check_taints_tolerations([], node)
        assert result.satisfied is False
        assert "dedicated" in result.reasoning

    def test_tolerated_equal_operator_passes(self):
        node = _node(taints=[NodeTaint(key="dedicated", value="gpu", effect="NoSchedule")])
        toleration = V1Toleration(key="dedicated", operator="Equal", value="gpu", effect="NoSchedule")
        result = check_taints_tolerations([toleration], node)
        assert result.satisfied is True

    def test_tolerated_exists_operator_ignores_value(self):
        node = _node(taints=[NodeTaint(key="dedicated", value="gpu", effect="NoSchedule")])
        toleration = V1Toleration(key="dedicated", operator="Exists", effect="NoSchedule")
        result = check_taints_tolerations([toleration], node)
        assert result.satisfied is True

    def test_wrong_value_with_equal_operator_does_not_tolerate(self):
        node = _node(taints=[NodeTaint(key="dedicated", value="gpu", effect="NoSchedule")])
        toleration = V1Toleration(key="dedicated", operator="Equal", value="cpu", effect="NoSchedule")
        result = check_taints_tolerations([toleration], node)
        assert result.satisfied is False

    def test_toleration_for_wrong_effect_does_not_tolerate(self):
        node = _node(taints=[NodeTaint(key="dedicated", value="gpu", effect="NoExecute")])
        toleration = V1Toleration(key="dedicated", operator="Equal", value="gpu", effect="NoSchedule")
        result = check_taints_tolerations([toleration], node)
        assert result.satisfied is False

    def test_untolerated_prefer_no_schedule_is_soft_not_blocking(self):
        node = _node(taints=[NodeTaint(key="spot", value="true", effect="PreferNoSchedule")])
        result = check_taints_tolerations([], node)
        assert result.satisfied is True
        assert "spot" in result.reasoning

    def test_prefer_no_schedule_does_not_mask_a_real_blocking_taint(self):
        node = _node(
            taints=[
                NodeTaint(key="spot", value="true", effect="PreferNoSchedule"),
                NodeTaint(key="dedicated", value="gpu", effect="NoSchedule"),
            ]
        )
        result = check_taints_tolerations([], node)
        assert result.satisfied is False
        assert "dedicated" in result.reasoning


class TestNodeSelector:
    def test_no_selector_is_satisfied(self):
        node = _node(labels={})
        assert check_node_selector(None, node).satisfied is True
        assert check_node_selector({}, node).satisfied is True

    def test_matching_selector_is_satisfied(self):
        node = _node(labels={"disktype": "ssd"})
        result = check_node_selector({"disktype": "ssd"}, node)
        assert result.satisfied is True

    def test_mismatched_value_fails(self):
        node = _node(labels={"disktype": "hdd"})
        result = check_node_selector({"disktype": "ssd"}, node)
        assert result.satisfied is False
        assert "disktype" in result.reasoning

    def test_missing_label_fails(self):
        node = _node(labels={})
        result = check_node_selector({"disktype": "ssd"}, node)
        assert result.satisfied is False

    def test_multiple_keys_all_must_match(self):
        node = _node(labels={"disktype": "ssd", "zone": "a"})
        assert check_node_selector({"disktype": "ssd", "zone": "a"}, node).satisfied is True
        assert check_node_selector({"disktype": "ssd", "zone": "b"}, node).satisfied is False


def _required_affinity(*terms: V1NodeSelectorTerm) -> V1Affinity:
    return V1Affinity(
        node_affinity=V1NodeAffinity(
            required_during_scheduling_ignored_during_execution=V1NodeSelector(node_selector_terms=list(terms))
        )
    )


class TestRequiredNodeAffinity:
    def test_no_affinity_is_satisfied(self):
        node = _node(labels={})
        assert check_required_node_affinity(None, node).satisfied is True

    def test_in_operator_matches(self):
        node = _node(labels={"zone": "us-east-1a"})
        affinity = _required_affinity(
            V1NodeSelectorTerm(match_expressions=[V1NodeSelectorRequirement(key="zone", operator="In", values=["us-east-1a", "us-east-1b"])])
        )
        assert check_required_node_affinity(affinity, node).satisfied is True

    def test_in_operator_does_not_match(self):
        node = _node(labels={"zone": "us-west-2a"})
        affinity = _required_affinity(
            V1NodeSelectorTerm(match_expressions=[V1NodeSelectorRequirement(key="zone", operator="In", values=["us-east-1a"])])
        )
        assert check_required_node_affinity(affinity, node).satisfied is False

    def test_not_in_operator(self):
        node = _node(labels={"zone": "us-west-2a"})
        affinity = _required_affinity(
            V1NodeSelectorTerm(match_expressions=[V1NodeSelectorRequirement(key="zone", operator="NotIn", values=["us-east-1a"])])
        )
        assert check_required_node_affinity(affinity, node).satisfied is True

    def test_exists_operator(self):
        node = _node(labels={"gpu": "true"})
        affinity = _required_affinity(V1NodeSelectorTerm(match_expressions=[V1NodeSelectorRequirement(key="gpu", operator="Exists")]))
        assert check_required_node_affinity(affinity, node).satisfied is True

    def test_does_not_exist_operator(self):
        node = _node(labels={})
        affinity = _required_affinity(V1NodeSelectorTerm(match_expressions=[V1NodeSelectorRequirement(key="gpu", operator="DoesNotExist")]))
        assert check_required_node_affinity(affinity, node).satisfied is True

    def test_gt_operator(self):
        node = _node(labels={"cpu-count": "16"})
        affinity = _required_affinity(V1NodeSelectorTerm(match_expressions=[V1NodeSelectorRequirement(key="cpu-count", operator="Gt", values=["8"])]))
        assert check_required_node_affinity(affinity, node).satisfied is True

    def test_lt_operator_fails_when_not_less(self):
        node = _node(labels={"cpu-count": "16"})
        affinity = _required_affinity(V1NodeSelectorTerm(match_expressions=[V1NodeSelectorRequirement(key="cpu-count", operator="Lt", values=["8"])]))
        assert check_required_node_affinity(affinity, node).satisfied is False

    def test_multiple_terms_are_ored(self):
        node = _node(labels={"zone": "us-west-2a"})
        affinity = _required_affinity(
            V1NodeSelectorTerm(match_expressions=[V1NodeSelectorRequirement(key="zone", operator="In", values=["us-east-1a"])]),
            V1NodeSelectorTerm(match_expressions=[V1NodeSelectorRequirement(key="zone", operator="In", values=["us-west-2a"])]),
        )
        assert check_required_node_affinity(affinity, node).satisfied is True

    def test_multiple_expressions_within_one_term_are_anded(self):
        node = _node(labels={"zone": "us-west-2a", "disktype": "hdd"})
        affinity = _required_affinity(
            V1NodeSelectorTerm(
                match_expressions=[
                    V1NodeSelectorRequirement(key="zone", operator="In", values=["us-west-2a"]),
                    V1NodeSelectorRequirement(key="disktype", operator="In", values=["ssd"]),
                ]
            )
        )
        assert check_required_node_affinity(affinity, node).satisfied is False


class TestNamespaceQuota:
    def test_no_quotas_is_satisfied(self):
        result = check_namespace_quota(ResourceQuantity(cpu_millicores=100, memory_bytes=100), [])
        assert result.satisfied is True

    def test_within_quota_is_satisfied(self):
        quota = V1ResourceQuota(
            metadata=V1ObjectMeta(name="compute-quota"),
            status=V1ResourceQuotaStatus(hard={"requests.cpu": "2"}, used={"requests.cpu": "500m"}),
        )
        result = check_namespace_quota(ResourceQuantity(cpu_millicores=500, memory_bytes=None), [quota])
        assert result.satisfied is True

    def test_exceeding_quota_fails(self):
        quota = V1ResourceQuota(
            metadata=V1ObjectMeta(name="compute-quota"),
            status=V1ResourceQuotaStatus(hard={"requests.cpu": "1"}, used={"requests.cpu": "900m"}),
        )
        result = check_namespace_quota(ResourceQuantity(cpu_millicores=500, memory_bytes=None), [quota])
        assert result.satisfied is False
        assert "compute-quota" in result.reasoning


class TestLabelSelectorMatches:
    def test_null_selector_matches_nothing(self):
        assert label_selector_matches(None, {"app": "demo"}) is False

    def test_empty_present_selector_matches_everything(self):
        assert label_selector_matches(V1LabelSelector(), {"app": "demo"}) is True

    def test_match_labels_matches(self):
        assert label_selector_matches(V1LabelSelector(match_labels={"app": "demo"}), {"app": "demo"}) is True

    def test_match_labels_does_not_match(self):
        assert label_selector_matches(V1LabelSelector(match_labels={"app": "other"}), {"app": "demo"}) is False

    def test_match_expressions_in_operator(self):
        selector = V1LabelSelector(match_expressions=[V1LabelSelectorRequirement(key="app", operator="In", values=["demo"])])
        assert label_selector_matches(selector, {"app": "demo"}) is True


def _pod_label_summary(**overrides) -> PodLabelSummary:
    defaults = dict(uid="pod-uid-1", namespace="default", node_name="node-1", labels={})
    defaults.update(overrides)
    return PodLabelSummary(**defaults)


def _required_pod_affinity(*terms: V1PodAffinityTerm) -> V1Affinity:
    return V1Affinity(pod_affinity=V1PodAffinity(required_during_scheduling_ignored_during_execution=list(terms)))


def _required_pod_anti_affinity(*terms: V1PodAffinityTerm) -> V1Affinity:
    return V1Affinity(pod_anti_affinity=V1PodAntiAffinity(required_during_scheduling_ignored_during_execution=list(terms)))


class TestCheckPodAffinity:
    def test_no_affinity_is_satisfied(self):
        node = _node(labels={"zone": "a"})
        result = check_pod_affinity(None, candidate_namespace="default", candidate_uid=None, node=node, nodes=[node], pods=[])
        assert result.constraint_type == ConstraintType.REQUIRED_POD_AFFINITY
        assert result.satisfied is True

    def test_matching_pod_in_same_domain_satisfies(self):
        node = _node(name="node-1", labels={"zone": "a"})
        other_node = _node(name="node-2", labels={"zone": "a"})
        affinity = _required_pod_affinity(
            V1PodAffinityTerm(topology_key="zone", label_selector=V1LabelSelector(match_labels={"app": "cache"}))
        )
        pods = [_pod_label_summary(uid="other", namespace="default", node_name="node-2", labels={"app": "cache"})]
        result = check_pod_affinity(
            affinity, candidate_namespace="default", candidate_uid=None, node=node, nodes=[node, other_node], pods=pods
        )
        assert result.satisfied is True

    def test_no_matching_pod_fails(self):
        node = _node(labels={"zone": "a"})
        affinity = _required_pod_affinity(
            V1PodAffinityTerm(topology_key="zone", label_selector=V1LabelSelector(match_labels={"app": "cache"}))
        )
        result = check_pod_affinity(affinity, candidate_namespace="default", candidate_uid=None, node=node, nodes=[node], pods=[])
        assert result.satisfied is False

    def test_multiple_terms_are_anded(self):
        node = _node(name="node-1", labels={"zone": "a", "rack": "r1"})
        affinity = _required_pod_affinity(
            V1PodAffinityTerm(topology_key="zone", label_selector=V1LabelSelector(match_labels={"app": "cache"})),
            V1PodAffinityTerm(topology_key="rack", label_selector=V1LabelSelector(match_labels={"app": "missing"})),
        )
        pods = [_pod_label_summary(uid="other", namespace="default", node_name="node-1", labels={"app": "cache"})]
        result = check_pod_affinity(affinity, candidate_namespace="default", candidate_uid=None, node=node, nodes=[node], pods=pods)
        assert result.satisfied is False

    def test_candidate_pod_excluded_from_matching_itself(self):
        node = _node(labels={"zone": "a"})
        affinity = _required_pod_affinity(
            V1PodAffinityTerm(topology_key="zone", label_selector=V1LabelSelector(match_labels={"app": "cache"}))
        )
        pods = [_pod_label_summary(uid="self-uid", namespace="default", node_name="node-1", labels={"app": "cache"})]
        result = check_pod_affinity(
            affinity, candidate_namespace="default", candidate_uid="self-uid", node=node, nodes=[node], pods=pods
        )
        assert result.satisfied is False

    def test_namespace_selector_term_treated_as_not_satisfied(self):
        node = _node(labels={"zone": "a"})
        affinity = _required_pod_affinity(
            V1PodAffinityTerm(topology_key="zone", namespace_selector=V1LabelSelector())
        )
        result = check_pod_affinity(affinity, candidate_namespace="default", candidate_uid=None, node=node, nodes=[node], pods=[])
        assert result.satisfied is False
        assert "namespaceSelector" in result.reasoning


class TestCheckPodAntiAffinity:
    def test_no_affinity_is_satisfied(self):
        node = _node(labels={"zone": "a"})
        result = check_pod_anti_affinity(None, candidate_namespace="default", candidate_uid=None, node=node, nodes=[node], pods=[])
        assert result.constraint_type == ConstraintType.REQUIRED_POD_ANTI_AFFINITY
        assert result.satisfied is True

    def test_conflicting_pod_in_same_domain_fails(self):
        node = _node(labels={"zone": "a"})
        affinity = _required_pod_anti_affinity(
            V1PodAffinityTerm(topology_key="zone", label_selector=V1LabelSelector(match_labels={"app": "cache"}))
        )
        pods = [_pod_label_summary(uid="other", namespace="default", node_name="node-1", labels={"app": "cache"})]
        result = check_pod_anti_affinity(
            affinity, candidate_namespace="default", candidate_uid=None, node=node, nodes=[node], pods=pods
        )
        assert result.satisfied is False

    def test_no_conflicting_pod_is_satisfied(self):
        node = _node(labels={"zone": "a"})
        affinity = _required_pod_anti_affinity(
            V1PodAffinityTerm(topology_key="zone", label_selector=V1LabelSelector(match_labels={"app": "cache"}))
        )
        result = check_pod_anti_affinity(affinity, candidate_namespace="default", candidate_uid=None, node=node, nodes=[node], pods=[])
        assert result.satisfied is True

    def test_candidate_pod_excluded_from_matching_itself(self):
        node = _node(labels={"zone": "a"})
        affinity = _required_pod_anti_affinity(
            V1PodAffinityTerm(topology_key="zone", label_selector=V1LabelSelector(match_labels={"app": "cache"}))
        )
        pods = [_pod_label_summary(uid="self-uid", namespace="default", node_name="node-1", labels={"app": "cache"})]
        result = check_pod_anti_affinity(
            affinity, candidate_namespace="default", candidate_uid="self-uid", node=node, nodes=[node], pods=pods
        )
        assert result.satisfied is True

    def test_namespace_selector_term_treated_as_satisfied(self):
        node = _node(labels={"zone": "a"})
        affinity = _required_pod_anti_affinity(
            V1PodAffinityTerm(topology_key="zone", namespace_selector=V1LabelSelector())
        )
        result = check_pod_anti_affinity(affinity, candidate_namespace="default", candidate_uid=None, node=node, nodes=[node], pods=[])
        assert result.satisfied is True
        assert "namespaceSelector" in result.reasoning


class TestCheckTopologySpreadConstraints:
    def test_no_constraints_is_satisfied(self):
        node = _node(labels={"zone": "a"})
        result = check_topology_spread_constraints(None, candidate_namespace="default", candidate_uid=None, node=node, nodes=[node], pods=[])
        assert result.constraint_type == ConstraintType.TOPOLOGY_SPREAD
        assert result.satisfied is True

    def test_within_max_skew_is_satisfied(self):
        node_a = _node(name="node-a", labels={"zone": "a"})
        node_b = _node(name="node-b", labels={"zone": "b"})
        constraint = V1TopologySpreadConstraint(
            max_skew=1,
            topology_key="zone",
            when_unsatisfiable="DoNotSchedule",
            label_selector=V1LabelSelector(match_labels={"app": "web"}),
        )
        pods = [_pod_label_summary(uid="p1", namespace="default", node_name="node-a", labels={"app": "web"})]
        result = check_topology_spread_constraints(
            [constraint], candidate_namespace="default", candidate_uid=None, node=node_b, nodes=[node_a, node_b], pods=pods
        )
        assert result.satisfied is True

    def test_exceeding_max_skew_with_do_not_schedule_blocks(self):
        node_a = _node(name="node-a", labels={"zone": "a"})
        node_b = _node(name="node-b", labels={"zone": "b"})
        constraint = V1TopologySpreadConstraint(
            max_skew=1,
            topology_key="zone",
            when_unsatisfiable="DoNotSchedule",
            label_selector=V1LabelSelector(match_labels={"app": "web"}),
        )
        pods = [
            _pod_label_summary(uid="p1", namespace="default", node_name="node-a", labels={"app": "web"}),
            _pod_label_summary(uid="p2", namespace="default", node_name="node-a", labels={"app": "web"}),
            _pod_label_summary(uid="p3", namespace="default", node_name="node-a", labels={"app": "web"}),
        ]
        result = check_topology_spread_constraints(
            [constraint], candidate_namespace="default", candidate_uid=None, node=node_b, nodes=[node_a, node_b], pods=pods
        )
        assert result.satisfied is False
        assert "zone" in result.reasoning

    def test_exceeding_max_skew_with_schedule_anyway_is_soft(self):
        node_a = _node(name="node-a", labels={"zone": "a"})
        node_b = _node(name="node-b", labels={"zone": "b"})
        constraint = V1TopologySpreadConstraint(
            max_skew=1,
            topology_key="zone",
            when_unsatisfiable="ScheduleAnyway",
            label_selector=V1LabelSelector(match_labels={"app": "web"}),
        )
        pods = [
            _pod_label_summary(uid="p1", namespace="default", node_name="node-a", labels={"app": "web"}),
            _pod_label_summary(uid="p2", namespace="default", node_name="node-a", labels={"app": "web"}),
            _pod_label_summary(uid="p3", namespace="default", node_name="node-a", labels={"app": "web"}),
        ]
        result = check_topology_spread_constraints(
            [constraint], candidate_namespace="default", candidate_uid=None, node=node_b, nodes=[node_a, node_b], pods=pods
        )
        assert result.satisfied is True
        assert "zone" in result.reasoning

    def test_candidate_pod_excluded_from_its_own_count(self):
        node_a = _node(name="node-a", labels={"zone": "a"})
        node_b = _node(name="node-b", labels={"zone": "b"})
        constraint = V1TopologySpreadConstraint(
            max_skew=1,
            topology_key="zone",
            when_unsatisfiable="DoNotSchedule",
            label_selector=V1LabelSelector(match_labels={"app": "web"}),
        )
        pods = [_pod_label_summary(uid="self-uid", namespace="default", node_name="node-b", labels={"app": "web"})]
        result = check_topology_spread_constraints(
            [constraint],
            candidate_namespace="default",
            candidate_uid="self-uid",
            node=node_b,
            nodes=[node_a, node_b],
            pods=pods,
        )
        assert result.satisfied is True

    def test_candidate_node_missing_topology_label_blocks(self):
        node_a = _node(name="node-a", labels={"zone": "a"})
        node_b = _node(name="node-b", labels={})
        constraint = V1TopologySpreadConstraint(
            max_skew=1,
            topology_key="zone",
            when_unsatisfiable="DoNotSchedule",
            label_selector=V1LabelSelector(match_labels={"app": "web"}),
        )
        result = check_topology_spread_constraints(
            [constraint], candidate_namespace="default", candidate_uid=None, node=node_b, nodes=[node_a, node_b], pods=[]
        )
        assert result.satisfied is False

    def test_candidate_node_missing_topology_label_schedule_anyway_is_non_blocking(self):
        node_a = _node(name="node-a", labels={"zone": "a"})
        node_b = _node(name="node-b", labels={})
        constraint = V1TopologySpreadConstraint(
            max_skew=1,
            topology_key="zone",
            when_unsatisfiable="ScheduleAnyway",
            label_selector=V1LabelSelector(match_labels={"app": "web"}),
        )
        result = check_topology_spread_constraints(
            [constraint], candidate_namespace="default", candidate_uid=None, node=node_b, nodes=[node_a, node_b], pods=[]
        )
        assert result.satisfied is True

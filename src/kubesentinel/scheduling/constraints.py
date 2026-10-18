"""Pure constraint-evaluation functions: `(pod spec facts, one NodeSnapshot)
-> ConstraintResult`. No network calls, no cluster access, no side effects
-- the same "pure function over already-fetched facts" shape
`optimization/safety.py` and `policy/engine.py` already use.

Only the constraints in `models.scheduling.EVALUATED_CONSTRAINT_TYPES` get
a function here. Everything else (the preferred half of node/pod affinity,
and any pod affinity/anti-affinity term this engine can't evaluate) is
detected by `analyzer.py` directly from the Pod spec and reported as
unevaluated -- see docs/SCHEDULING_ANALYSIS.md Section 2 for why.
"""

from __future__ import annotations

from kubesentinel.models.optimization import ResourceQuantity
from kubesentinel.models.scheduling import ConstraintResult, ConstraintType, NodeSnapshot, PodLabelSummary
from kubesentinel.optimization.quantities import QuantityParseError, parse_cpu_to_millicores, parse_memory_to_bytes


def check_resource_fit(pod_request: ResourceQuantity, node: NodeSnapshot) -> ConstraintResult:
    cpu_alloc = node.allocatable.cpu_millicores
    mem_alloc = node.allocatable.memory_bytes
    cpu_req = node.requested.cpu_millicores or 0
    mem_req = node.requested.memory_bytes or 0
    pod_cpu = pod_request.cpu_millicores or 0
    pod_mem = pod_request.memory_bytes or 0

    shortfalls = []
    if cpu_alloc is not None:
        remaining_cpu = cpu_alloc - cpu_req
        if pod_cpu > remaining_cpu:
            shortfalls.append(
                f"cpu: node has {cpu_alloc}m allocatable, {cpu_req}m already requested by other "
                f"Pods, this Pod requests {pod_cpu}m -- short by {pod_cpu - remaining_cpu}m"
            )
    if mem_alloc is not None:
        remaining_mem = mem_alloc - mem_req
        if pod_mem > remaining_mem:
            shortfalls.append(
                f"memory: node has {mem_alloc} bytes allocatable, {mem_req} already requested by "
                f"other Pods, this Pod requests {pod_mem} -- short by {pod_mem - remaining_mem} bytes"
            )

    if shortfalls:
        return ConstraintResult(
            constraint_type=ConstraintType.RESOURCE_FIT, satisfied=False, reasoning="; ".join(shortfalls)
        )
    return ConstraintResult(
        constraint_type=ConstraintType.RESOURCE_FIT,
        satisfied=True,
        reasoning=(
            f"cpu: {cpu_req}m + {pod_cpu}m <= {cpu_alloc}m allocatable; "
            f"memory: {mem_req} + {pod_mem} <= {mem_alloc} bytes allocatable"
        ),
    )


def _toleration_matches(taint, tolerations: list) -> bool:
    for tol in tolerations:
        if tol.effect and tol.effect != taint.effect:
            continue
        operator = tol.operator or "Equal"
        if operator == "Exists":
            if tol.key is None or tol.key == taint.key:
                return True
        elif operator == "Equal":
            if tol.key == taint.key and (tol.value or "") == (taint.value or ""):
                return True
    return False


def check_taints_tolerations(tolerations: list, node: NodeSnapshot) -> ConstraintResult:
    """`tolerations` is the raw list from `pod.spec.tolerations` (may be
    None/empty). `PreferNoSchedule` taints are reported but never cause
    `satisfied=False` -- they're a soft deterrent the scheduler's scoring
    phase weighs, not a hard block (matching real scheduler semantics)."""
    tolerations = tolerations or []
    blocking = []
    soft = []
    for taint in node.taints:
        tolerated = _toleration_matches(taint, tolerations)
        if tolerated:
            continue
        label = f"{taint.key}={taint.value or ''}:{taint.effect}"
        if taint.effect in ("NoSchedule", "NoExecute"):
            blocking.append(label)
        elif taint.effect == "PreferNoSchedule":
            soft.append(label)

    if blocking:
        reasoning = f"untolerated blocking taint(s): {', '.join(blocking)}"
        if soft:
            reasoning += f" (also untolerated but non-blocking: {', '.join(soft)})"
        return ConstraintResult(constraint_type=ConstraintType.TAINT_TOLERATION, satisfied=False, reasoning=reasoning)
    if soft:
        return ConstraintResult(
            constraint_type=ConstraintType.TAINT_TOLERATION,
            satisfied=True,
            reasoning=f"all blocking taints tolerated; untolerated PreferNoSchedule (soft, non-blocking): {', '.join(soft)}",
        )
    return ConstraintResult(
        constraint_type=ConstraintType.TAINT_TOLERATION,
        satisfied=True,
        reasoning="no taints on this node" if not node.taints else "all taints tolerated",
    )


def check_node_selector(node_selector: dict | None, node: NodeSnapshot) -> ConstraintResult:
    node_selector = node_selector or {}
    if not node_selector:
        return ConstraintResult(
            constraint_type=ConstraintType.NODE_SELECTOR, satisfied=True, reasoning="no nodeSelector on this Pod"
        )
    mismatches = [
        f"{k}={v!r} (node has {node.labels.get(k)!r})"
        for k, v in node_selector.items()
        if node.labels.get(k) != v
    ]
    if mismatches:
        return ConstraintResult(
            constraint_type=ConstraintType.NODE_SELECTOR,
            satisfied=False,
            reasoning=f"nodeSelector not matched: {', '.join(mismatches)}",
        )
    return ConstraintResult(
        constraint_type=ConstraintType.NODE_SELECTOR,
        satisfied=True,
        reasoning=f"nodeSelector matched: {node_selector}",
    )


def requirement_matches(req, labels: dict) -> bool:
    """Evaluates one `In`/`NotIn`/`Exists`/`DoesNotExist`/`Gt`/`Lt`
    requirement against a label dict. Public specifically so
    `network/policy_inventory.py` can reuse it for `V1LabelSelectorRequirement`
    (NetworkPolicy's `podSelector`) -- structurally identical to the
    `V1NodeSelectorRequirement` shape this was written for (same four field
    names), even though label selectors only ever use the first four
    operators in practice; `Gt`/`Lt` simply never fire for that caller."""
    key = req.key
    values = req.values or []
    operator = req.operator
    actual = labels.get(key)
    if operator == "In":
        return actual is not None and actual in values
    if operator == "NotIn":
        return actual is None or actual not in values
    if operator == "Exists":
        return key in labels
    if operator == "DoesNotExist":
        return key not in labels
    if operator in ("Gt", "Lt") and actual is not None and values:
        try:
            actual_int, target_int = int(actual), int(values[0])
        except ValueError:
            return False
        return actual_int > target_int if operator == "Gt" else actual_int < target_int
    return False


def check_namespace_quota(pod_request: ResourceQuantity, quotas: list) -> ConstraintResult:
    """`quotas` is the raw list of `V1ResourceQuota` objects for the
    namespace (may be empty). Evaluated once per analysis, not per node --
    a quota is a namespace-wide admission check, not a placement constraint,
    so it appears once in a finding, not once per `NodeFitResult`. Only
    `cpu`/`memory` request quotas are checked (not `limits.cpu`/
    `limits.memory`/object-count quotas -- a documented v1 scope, not
    silently ignored: see docs/SCHEDULING_ANALYSIS.md)."""
    if not quotas:
        return ConstraintResult(
            constraint_type=ConstraintType.NAMESPACE_QUOTA, satisfied=True, reasoning="no ResourceQuota in this namespace"
        )

    violations = []
    for quota in quotas:
        hard = quota.status.hard if quota.status else None
        used = quota.status.used if quota.status else None
        if not hard:
            continue
        for key, request_value in (("requests.cpu", pod_request.cpu_millicores), ("requests.memory", pod_request.memory_bytes)):
            if request_value is None or key not in hard:
                continue
            parse = parse_cpu_to_millicores if key == "requests.cpu" else parse_memory_to_bytes
            try:
                hard_value = parse(hard[key])
                used_value = parse(used.get(key, "0")) if used else 0
            except QuantityParseError:
                continue
            if used_value + request_value > hard_value:
                violations.append(
                    f"{quota.metadata.name}/{key}: {used_value} used + {request_value} requested > {hard_value} hard limit"
                )

    if violations:
        return ConstraintResult(
            constraint_type=ConstraintType.NAMESPACE_QUOTA, satisfied=False, reasoning="; ".join(violations)
        )
    return ConstraintResult(
        constraint_type=ConstraintType.NAMESPACE_QUOTA, satisfied=True, reasoning="within every ResourceQuota in this namespace"
    )


def check_required_node_affinity(affinity, node: NodeSnapshot) -> ConstraintResult:
    """`affinity` is the raw `pod.spec.affinity` (may be None). Only
    `nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution` is
    evaluated -- `matchFields` (not `matchLabels`) within a term is not
    supported (rare in practice; a term using only `matchFields` is treated
    as not satisfied, conservatively, rather than silently skipped)."""
    required = None
    if affinity and affinity.node_affinity:
        required = affinity.node_affinity.required_during_scheduling_ignored_during_execution
    if required is None or not required.node_selector_terms:
        return ConstraintResult(
            constraint_type=ConstraintType.REQUIRED_NODE_AFFINITY,
            satisfied=True,
            reasoning="no required nodeAffinity on this Pod",
        )

    term_results = []
    for term in required.node_selector_terms:
        expressions = term.match_expressions or []
        if term.match_fields:
            term_results.append((False, "term uses matchFields, which this engine does not evaluate"))
            continue
        if not expressions:
            term_results.append((True, "empty term (matches everything)"))
            continue
        failed = [f"{e.key} {e.operator} {e.values}" for e in expressions if not requirement_matches(e, node.labels)]
        if failed:
            term_results.append((False, f"unmatched: {', '.join(failed)}"))
        else:
            term_results.append((True, "all expressions matched"))

    # nodeSelectorTerms are OR'd: satisfied if ANY term is satisfied.
    if any(satisfied for satisfied, _ in term_results):
        return ConstraintResult(
            constraint_type=ConstraintType.REQUIRED_NODE_AFFINITY, satisfied=True, reasoning="a required nodeAffinity term matched"
        )
    reasons = "; ".join(reason for _, reason in term_results)
    return ConstraintResult(
        constraint_type=ConstraintType.REQUIRED_NODE_AFFINITY,
        satisfied=False,
        reasoning=f"no required nodeAffinity term matched -- {reasons}",
    )


def label_selector_matches(selector, labels: dict) -> bool:
    """Standard Kubernetes LabelSelector semantics (API conventions): "a
    null label selector matches no objects, while an empty label selector
    matches all objects." Promoted here from
    `security/rules/availability.py::_pdb_selector_matches` -- same
    algorithm, now reused by a third caller (pod affinity/anti-affinity
    below), so it belongs next to `requirement_matches`, the primitive it
    already builds on. This is the *opposite* null-handling from
    `network/policy_inventory.py`'s own `podSelector` matching, which
    defensively treats null as "matches all" for a documented,
    NetworkPolicy-specific reason -- never reuse that one here."""
    if selector is None:
        return False
    match_labels = selector.match_labels or {}
    match_expressions = selector.match_expressions or []
    if not match_labels and not match_expressions:
        return True
    if not all(labels.get(k) == v for k, v in match_labels.items()):
        return False
    return all(requirement_matches(req, labels) for req in match_expressions)


def _topology_domain_pods(
    term_namespaces: list[str],
    label_selector,
    topology_key: str,
    topology_value: str,
    nodes_by_name: dict[str, NodeSnapshot],
    pods: list[PodLabelSummary],
    exclude_uid: str | None,
) -> list[PodLabelSummary]:
    """Every Pod (other than `exclude_uid`, the candidate Pod itself, when
    it already exists) in one of `term_namespaces`, matching
    `label_selector`, whose own Node shares `topology_value` for
    `topology_key` with the candidate node."""
    matches = []
    for pod in pods:
        if exclude_uid and pod.uid == exclude_uid:
            continue
        if pod.namespace not in term_namespaces:
            continue
        if not label_selector_matches(label_selector, pod.labels):
            continue
        pod_node = nodes_by_name.get(pod.node_name)
        if pod_node is None or pod_node.labels.get(topology_key) != topology_value:
            continue
        matches.append(pod)
    return matches


def _evaluate_pod_affinity_terms(
    terms: list,
    *,
    candidate_namespace: str,
    candidate_uid: str | None,
    node: NodeSnapshot,
    nodes_by_name: dict[str, NodeSnapshot],
    pods: list[PodLabelSummary],
    want_match: bool,
) -> tuple[bool, list[str]]:
    """Shared term-evaluation loop for affinity (`want_match=True`: every
    term needs a matching Pod to exist) and anti-affinity
    (`want_match=False`: every term needs NO matching Pod to exist).
    Required terms are ANDed -- unlike node affinity's `nodeSelectorTerms`,
    which are OR'd -- so one failing/unevaluatable term fails the whole
    constraint. Returns `(satisfied, reasons)`."""
    reasons: list[str] = []
    satisfied = True
    for term in terms:
        if term.namespace_selector is not None:
            # Can't evaluate without fetching every real Namespace object's
            # own labels -- not done here (see
            # docs/SCHEDULING_ANALYSIS.md Section 2's own scope line for
            # this engine). Conservative per direction: an affinity term
            # that can't be proven true is reported as not satisfied
            # (never guess a required condition is met); an anti-affinity
            # term that can't be proven *violated* is reported as
            # satisfied (never fabricate a violation it hasn't earned the
            # evidence for).
            reasons.append(f"term with topologyKey={term.topology_key!r} uses namespaceSelector, not evaluated")
            if want_match:
                satisfied = False
            continue

        topology_value = node.labels.get(term.topology_key)
        if topology_value is None:
            reasons.append(f"candidate node has no label {term.topology_key!r}")
            if want_match:
                satisfied = False
            continue

        term_namespaces = list(term.namespaces) if term.namespaces else [candidate_namespace]
        matching_pods = _topology_domain_pods(
            term_namespaces, term.label_selector, term.topology_key, topology_value, nodes_by_name, pods, candidate_uid
        )
        found = len(matching_pods) > 0
        if found == want_match:
            reasons.append(
                f"topologyKey={term.topology_key!r}: "
                + (f"found {len(matching_pods)} matching Pod(s)" if found else "no matching Pod found")
            )
        else:
            satisfied = False
            reasons.append(
                f"topologyKey={term.topology_key!r}: "
                + (
                    f"found {len(matching_pods)} matching Pod(s), expected none (anti-affinity)"
                    if found
                    else "no matching Pod found, required one (affinity)"
                )
            )
    return satisfied, reasons


def check_pod_affinity(
    affinity,
    *,
    candidate_namespace: str,
    candidate_uid: str | None,
    node: NodeSnapshot,
    nodes: list[NodeSnapshot],
    pods: list[PodLabelSummary],
) -> ConstraintResult:
    """Only `podAffinity.requiredDuringSchedulingIgnoredDuringExecution` --
    the preferred half stays detected-not-evaluated (`POD_AFFINITY` in
    `unevaluated_constraints`), the same required-vs-preferred split
    `check_required_node_affinity` already established."""
    required = affinity.pod_affinity.required_during_scheduling_ignored_during_execution if affinity and affinity.pod_affinity else None
    if not required:
        return ConstraintResult(
            constraint_type=ConstraintType.REQUIRED_POD_AFFINITY, satisfied=True, reasoning="no required podAffinity on this Pod"
        )
    nodes_by_name = {n.name: n for n in nodes}
    satisfied, reasons = _evaluate_pod_affinity_terms(
        required,
        candidate_namespace=candidate_namespace,
        candidate_uid=candidate_uid,
        node=node,
        nodes_by_name=nodes_by_name,
        pods=pods,
        want_match=True,
    )
    return ConstraintResult(
        constraint_type=ConstraintType.REQUIRED_POD_AFFINITY, satisfied=satisfied, reasoning="; ".join(reasons)
    )


def check_pod_anti_affinity(
    affinity,
    *,
    candidate_namespace: str,
    candidate_uid: str | None,
    node: NodeSnapshot,
    nodes: list[NodeSnapshot],
    pods: list[PodLabelSummary],
) -> ConstraintResult:
    """Only `podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution`
    -- same required-vs-preferred split as `check_pod_affinity`."""
    required = (
        affinity.pod_anti_affinity.required_during_scheduling_ignored_during_execution
        if affinity and affinity.pod_anti_affinity
        else None
    )
    if not required:
        return ConstraintResult(
            constraint_type=ConstraintType.REQUIRED_POD_ANTI_AFFINITY,
            satisfied=True,
            reasoning="no required podAntiAffinity on this Pod",
        )
    nodes_by_name = {n.name: n for n in nodes}
    satisfied, reasons = _evaluate_pod_affinity_terms(
        required,
        candidate_namespace=candidate_namespace,
        candidate_uid=candidate_uid,
        node=node,
        nodes_by_name=nodes_by_name,
        pods=pods,
        want_match=False,
    )
    return ConstraintResult(
        constraint_type=ConstraintType.REQUIRED_POD_ANTI_AFFINITY, satisfied=satisfied, reasoning="; ".join(reasons)
    )


def check_topology_spread_constraints(
    topology_spread_constraints: list | None,
    *,
    candidate_namespace: str,
    candidate_uid: str | None,
    node: NodeSnapshot,
    nodes: list[NodeSnapshot],
    pods: list[PodLabelSummary],
) -> ConstraintResult:
    """Evaluates every constraint's own `maxSkew`/`topologyKey`/
    `labelSelector`/`whenUnsatisfiable` -- `minDomains`, `matchLabelKeys`,
    `nodeAffinityPolicy`, and `nodeTaintsPolicy` (newer, less commonly set
    fields) are not evaluated, a documented v1 simplification, not an
    oversight. `whenUnsatisfiable=ScheduleAnyway` is a soft preference,
    reported but never blocking -- the same soft-vs-hard split
    `check_taints_tolerations`'s own `PreferNoSchedule` handling already
    established."""
    if not topology_spread_constraints:
        return ConstraintResult(
            constraint_type=ConstraintType.TOPOLOGY_SPREAD, satisfied=True, reasoning="no topologySpreadConstraints on this Pod"
        )

    nodes_by_name = {n.name: n for n in nodes}
    blocking: list[str] = []
    soft: list[str] = []
    for constraint in topology_spread_constraints:
        topology_key = constraint.topology_key
        max_skew = constraint.max_skew or 1
        candidate_value = node.labels.get(topology_key)
        if candidate_value is None:
            label = f"topologyKey={topology_key!r}: candidate node has no such label"
            if (constraint.when_unsatisfiable or "DoNotSchedule") == "DoNotSchedule":
                blocking.append(label)
            else:
                soft.append(label)
            continue

        # Count matching Pods per topology domain -- every domain any
        # candidate Node currently has a value for, not just this one, so
        # the skew calculation reflects the real spread across the whole
        # cluster, not just this node's own domain.
        counts: dict[str, int] = {}
        for domain_node in nodes:
            value = domain_node.labels.get(topology_key)
            if value is not None:
                counts.setdefault(value, 0)
        for pod in pods:
            if candidate_uid and pod.uid == candidate_uid:
                continue
            if pod.namespace != candidate_namespace:
                continue
            if not label_selector_matches(constraint.label_selector, pod.labels):
                continue
            pod_node = nodes_by_name.get(pod.node_name)
            if pod_node is None:
                continue
            value = pod_node.labels.get(topology_key)
            if value is not None:
                counts[value] = counts.get(value, 0) + 1

        counts[candidate_value] = counts.get(candidate_value, 0) + 1  # the candidate Pod itself, hypothetically placed
        skew = max(counts.values()) - min(counts.values())
        if skew <= max_skew:
            continue
        label = (
            f"topologyKey={topology_key!r}: placing here makes skew {skew} > maxSkew {max_skew} "
            f"(domain counts: {counts})"
        )
        if (constraint.when_unsatisfiable or "DoNotSchedule") == "DoNotSchedule":
            blocking.append(label)
        else:
            soft.append(label)

    if blocking:
        reasoning = f"blocking topology spread violation(s): {'; '.join(blocking)}"
        if soft:
            reasoning += f" (also over skew but ScheduleAnyway, non-blocking: {'; '.join(soft)})"
        return ConstraintResult(constraint_type=ConstraintType.TOPOLOGY_SPREAD, satisfied=False, reasoning=reasoning)
    if soft:
        return ConstraintResult(
            constraint_type=ConstraintType.TOPOLOGY_SPREAD,
            satisfied=True,
            reasoning=f"within maxSkew on every DoNotSchedule constraint; over skew but non-blocking (ScheduleAnyway): {'; '.join(soft)}",
        )
    return ConstraintResult(
        constraint_type=ConstraintType.TOPOLOGY_SPREAD, satisfied=True, reasoning="within maxSkew on every topologySpreadConstraint"
    )

"""Pure constraint-evaluation functions: `(pod spec facts, one NodeSnapshot)
-> ConstraintResult`. No network calls, no cluster access, no side effects
-- the same "pure function over already-fetched facts" shape
`optimization/safety.py` and `policy/engine.py` already use.

Only the constraints in `models.scheduling.EVALUATED_CONSTRAINT_TYPES` get
a function here. Everything else (pod affinity/anti-affinity, topology
spread, preferred node affinity) is detected by `analyzer.py` directly from
the Pod spec and reported as unevaluated -- see docs/SCHEDULING_ANALYSIS.md
Section 2 for why.
"""

from __future__ import annotations

from kubesentinel.models.optimization import ResourceQuantity
from kubesentinel.models.scheduling import ConstraintResult, ConstraintType, NodeSnapshot
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


def _requirement_matches(req, labels: dict) -> bool:
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
        failed = [f"{e.key} {e.operator} {e.values}" for e in expressions if not _requirement_matches(e, node.labels)]
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

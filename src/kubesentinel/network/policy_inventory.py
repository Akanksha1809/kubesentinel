"""Static NetworkPolicy analysis: which policies in a namespace apply to a
target Pod (via `podSelector`), and a plain-language summary of what each
one's rules say -- never a claim about actually-observed enforcement. See
docs/NETWORK_DIAGNOSTICS.md Section 2/11 for why that distinction is
load-bearing on this project's own lab cluster (kindnet does not enforce
NetworkPolicy).
"""

from __future__ import annotations

from kubernetes.client import V1NetworkPolicy, V1Pod

from kubesentinel.models.network import NetworkPolicyRelevance
from kubesentinel.observer.client import ClusterClients
from kubesentinel.scheduling.constraints import requirement_matches


def _pod_selector_matches(selector, pod: V1Pod) -> bool:
    """An empty/absent `podSelector` (`{}`) means "every Pod in this
    namespace" in real NetworkPolicy semantics -- the opposite of a
    Service's empty selector (which matches nothing). Handled explicitly,
    not left to an accidental `all([]) == True` fallthrough."""
    labels = pod.metadata.labels or {}
    if selector is None:
        return True
    match_labels = selector.match_labels or {}
    match_expressions = selector.match_expressions or []
    if not match_labels and not match_expressions:
        return True
    if not all(labels.get(k) == v for k, v in match_labels.items()):
        return False
    return all(requirement_matches(req, labels) for req in match_expressions)


def has_unrestricted_ingress_rule(policy: V1NetworkPolicy) -> bool:
    """True when this policy has an Ingress rule with no `from`/`ports`
    restriction at all -- an effective allow-all despite the policy
    object existing. Public specifically so the security auditor's own
    `UNRESTRICTED_INGRESS_POLICY` check (docs/SECURITY_AUDITING.md
    Section 3.3) can reuse this exact classification rather than
    re-reading and re-interpreting NetworkPolicy objects a second time,
    per the platform spec's own instruction."""
    spec = policy.spec
    policy_types = spec.policy_types or ["Ingress"]
    if "Ingress" not in policy_types:
        return False
    return any(not (rule._from or rule.ports) for rule in (spec.ingress or []))


def summarize_policy(policy: V1NetworkPolicy) -> str:
    spec = policy.spec
    policy_types = spec.policy_types or ["Ingress"]  # k8s default when unset
    parts = [f"policyTypes={policy_types}"]

    if "Ingress" in policy_types:
        rules = spec.ingress or []
        if not rules:
            parts.append("ingress: no rules -- denies all ingress to matched Pods")
        else:
            parts.append(f"ingress: {len(rules)} rule(s) would permit matching traffic")

    if "Egress" in policy_types:
        rules = spec.egress or []
        if not rules:
            parts.append("egress: no rules -- denies all egress from matched Pods")
        else:
            parts.append(f"egress: {len(rules)} rule(s) would permit matching traffic")

    return "; ".join(parts)


def build_applicable_policies(clients: ClusterClients, namespace: str, target_pod: V1Pod) -> list[NetworkPolicyRelevance]:
    policies = clients.networking.list_namespaced_network_policy(namespace=namespace).items
    results = []
    for policy in policies:
        applies = _pod_selector_matches(policy.spec.pod_selector, target_pod)
        results.append(
            NetworkPolicyRelevance(
                policy_name=policy.metadata.name,
                applies_to_target=applies,
                policy_types=list(policy.spec.policy_types or ["Ingress"]),
                summary=summarize_policy(policy),
            )
        )
    return results

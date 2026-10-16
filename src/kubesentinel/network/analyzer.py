"""Stitches Service/EndpointSlice/port facts, NetworkPolicy relevance, and
DNS health into one `NetworkFinding` -- the `Client/Pod -> DNS -> Service
-> EndpointSlice -> destination Pod` request-path narrative the spec asks
for, built entirely from already-fetched facts (a pure function, no cluster
access of its own -- the same "pure function over already-fetched facts"
shape `optimization/safety.py`/`scheduling/analyzer.py` both use).

Classification priority (highest first): DNS_UNHEALTHY -- nothing else
matters if the cluster's own DNS is down -- then CONFIGURATION_ERROR (a
concrete, confirmed misconfiguration: an unresolved named targetPort, or
the EndpointSlice controller's output disagreeing with the independently
computed selector match), then MISSING_OR_UNREADY_ENDPOINTS (the target
Pod is correctly selected but not yet Ready -- not a config problem),
then POLICY_MAY_RESTRICT (everything else checks out, but an applicable
NetworkPolicy's rules would, as written, restrict traffic -- see Section 2
for why this is never phrased as a certainty), then HEALTHY. A target Pod
that no Service selects at all has nothing to validate -- INCONCLUSIVE,
not HEALTHY (that would imply something was checked and passed).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from kubesentinel.models.network import (
    DnsHealthResult,
    NetworkFinding,
    NetworkFindingClassification,
    NetworkPolicyRelevance,
    ServiceMatchResult,
)


def analyze_network(
    *,
    namespace: str,
    target_pod_name: str,
    dns_health: DnsHealthResult,
    service_results: list[ServiceMatchResult],
    applicable_policies: list[NetworkPolicyRelevance],
    workload_kind: str | None = None,
    workload_name: str | None = None,
    now: datetime | None = None,
) -> NetworkFinding:
    now = now or datetime.now(timezone.utc)
    reasoning_parts: list[str] = []

    selecting_services = [s for s in service_results if target_pod_name in s.matched_pod_names]

    if not dns_health.healthy:
        classification = NetworkFindingClassification.DNS_UNHEALTHY
        reasoning_parts.append(f"DNS unhealthy: {dns_health.reasoning}.")
    elif not selecting_services:
        classification = NetworkFindingClassification.INCONCLUSIVE
        reasoning_parts.append(
            f"No Service in '{namespace}' selects this Pod -- nothing to validate for inbound "
            "Service-based connectivity (this Pod may not be meant to receive Service traffic)."
        )
    else:
        config_errors = []
        unready = []
        for svc in selecting_services:
            unmatched_ports = [p for p in svc.port_results if not p.matched]
            if unmatched_ports or not svc.endpointslice_consistent:
                config_errors.append(svc)
            elif target_pod_name not in svc.ready_pod_names:
                unready.append(svc)

        if config_errors:
            classification = NetworkFindingClassification.CONFIGURATION_ERROR
            for svc in config_errors:
                bad_ports = [p.reasoning for p in svc.port_results if not p.matched]
                if bad_ports:
                    reasoning_parts.append(f"Service '{svc.service_name}': {'; '.join(bad_ports)}.")
                if not svc.endpointslice_consistent:
                    reasoning_parts.append(
                        f"Service '{svc.service_name}': the real EndpointSlice ready-endpoint count "
                        f"({svc.endpointslice_ready_count}) disagrees with the independently computed "
                        f"ready-Pod-selector-match count ({len(svc.ready_pod_names)}) -- possible "
                        "EndpointSlice controller lag, or a bug."
                    )
        elif unready:
            classification = NetworkFindingClassification.MISSING_OR_UNREADY_ENDPOINTS
            names = ", ".join(s.service_name for s in unready)
            reasoning_parts.append(
                f"This Pod matches the selector for {names} but is not yet Ready -- "
                "excluded from that Service's endpoints until it passes its readiness check."
            )
        else:
            restricting = [p for p in applicable_policies if p.applies_to_target]
            if restricting:
                classification = NetworkFindingClassification.POLICY_MAY_RESTRICT
                for p in restricting:
                    reasoning_parts.append(f"NetworkPolicy '{p.policy_name}' applies to this Pod: {p.summary}.")
            else:
                classification = NetworkFindingClassification.HEALTHY
                names = ", ".join(s.service_name for s in selecting_services)
                reasoning_parts.append(f"Selected by {names}, Ready, ports resolve, no applicable NetworkPolicy.")

    reasoning_parts.append(
        "This is evidence-based analysis of Kubernetes object state, not a confirmed end-to-end "
        "connectivity test -- API inspection cannot prove a packet actually arrives."
    )

    return NetworkFinding(
        finding_id=str(uuid.uuid4()),
        namespace=namespace,
        target_pod_name=target_pod_name,
        workload_kind=workload_kind,
        workload_name=workload_name,
        analyzed_at=now,
        dns_health=dns_health,
        service_results=service_results,
        applicable_network_policies=applicable_policies,
        classification=classification,
        reasoning=" ".join(reasoning_parts),
    )

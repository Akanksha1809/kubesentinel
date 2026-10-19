"""CoreDNS health: the `kube-system` Deployment's own readiness, and
whether the `kube-dns` Service has ready endpoints. A new, narrow,
explicitly-justified exception to this project's "lab namespace only" RBAC
boundary -- see docs/NETWORK_DIAGNOSTICS.md Section 7 and
rbac/network-dns-role.yaml's own docstring for why.

This answers "does the cluster's DNS server appear healthy," never "would
resolving this specific name succeed" -- that would need an active probe,
deliberately deferred (see docs/NETWORK_DIAGNOSTICS.md Section 2).

Every one of this function's three sub-reads already degrades to
`healthy=False` on its own `ApiException` rather than raising -- but a
read failure and a confirmed problem are different claims, so each
failure branch also sets `DnsHealthResult.check_succeeded=False`.
`network/service.py::analyze_pod` surfaces that distinction as a
`NetworkFinding.warnings` entry rather than letting a read failure
masquerade as a confirmed `DNS_UNHEALTHY` finding -- see
docs/EVIDENCE_COMPLETENESS.md §5.
"""

from __future__ import annotations

import json

from kubernetes.client.rest import ApiException

from kubesentinel.models.network import DnsHealthResult
from kubesentinel.observer.client import ClusterClients

_KUBE_SYSTEM = "kube-system"
_COREDNS_DEPLOYMENT = "coredns"
_KUBE_DNS_SERVICE = "kube-dns"


def check_dns_health(clients: ClusterClients) -> DnsHealthResult:
    try:
        deployment = clients.apps.read_namespaced_deployment(name=_COREDNS_DEPLOYMENT, namespace=_KUBE_SYSTEM)
    except ApiException as exc:
        return DnsHealthResult(
            healthy=False,
            check_succeeded=False,
            reasoning=f"could not read the '{_COREDNS_DEPLOYMENT}' Deployment in '{_KUBE_SYSTEM}': {exc.reason} (status={exc.status})",
        )

    # status.readyReplicas is omitempty -- absent (None), not present-as-zero,
    # when genuinely 0. Same normalization optimization/inventory.py already
    # applies for the identical reason (see KUBERNETES_CONCEPTS.md).
    ready = deployment.status.ready_replicas or 0
    desired = deployment.spec.replicas or 0

    try:
        service = clients.core.read_namespaced_service(name=_KUBE_DNS_SERVICE, namespace=_KUBE_SYSTEM)
        service_exists = service is not None
    except ApiException as exc:
        if exc.status == 404:
            service_exists = False
        else:
            return DnsHealthResult(
                coredns_ready_replicas=ready,
                coredns_desired_replicas=desired,
                healthy=False,
                check_succeeded=False,
                reasoning=f"could not read the '{_KUBE_DNS_SERVICE}' Service: {exc.reason} (status={exc.status})",
            )

    endpoint_count = 0
    if service_exists:
        try:
            raw = clients.discovery.list_namespaced_endpoint_slice(
                namespace=_KUBE_SYSTEM,
                label_selector=f"kubernetes.io/service-name={_KUBE_DNS_SERVICE}",
                _preload_content=False,
            )
            data = json.loads(raw.data)
            for slice_dict in data.get("items", []):
                endpoints = slice_dict.get("endpoints") or []
                endpoint_count += sum(1 for ep in endpoints if (ep.get("conditions") or {}).get("ready") is not False)
        except ApiException as exc:
            return DnsHealthResult(
                coredns_ready_replicas=ready,
                coredns_desired_replicas=desired,
                kube_dns_service_exists=service_exists,
                healthy=False,
                check_succeeded=False,
                reasoning=f"could not read EndpointSlices for '{_KUBE_DNS_SERVICE}': {exc.reason} (status={exc.status})",
            )

    healthy = ready > 0 and ready >= desired and service_exists and endpoint_count > 0
    if healthy:
        reasoning = f"coredns {ready}/{desired} ready; kube-dns Service has {endpoint_count} ready endpoint(s)"
    else:
        problems = []
        if ready == 0:
            problems.append("coredns has zero ready replicas")
        elif ready < desired:
            problems.append(f"coredns is only {ready}/{desired} ready")
        if not service_exists:
            problems.append("kube-dns Service not found")
        elif endpoint_count == 0:
            problems.append("kube-dns Service has zero ready endpoints")
        reasoning = "; ".join(problems)

    return DnsHealthResult(
        coredns_ready_replicas=ready,
        coredns_desired_replicas=desired,
        kube_dns_service_exists=service_exists,
        kube_dns_endpoint_count=endpoint_count,
        healthy=healthy,
        reasoning=reasoning,
    )

"""Network troubleshooting engine models: Service/EndpointSlice/port facts,
NetworkPolicy relevance, DNS health, and the overall finding for one
target Pod's inbound connectivity story.

See docs/NETWORK_DIAGNOSTICS.md Section 2 for exactly what's evaluated for
real (Service selector matching, EndpointSlice consistency, port
resolution, NetworkPolicy static analysis, CoreDNS health) vs. explicitly
deferred (the active DNS/connectivity probe, Ingress/Gateway -- neither
applicable on this lab cluster today).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class NetworkFindingClassification(str, Enum):
    """The request-path narrative's own conclusion -- a classification of
    what the *evidence* shows, never a claim about observed packet
    connectivity. `POLICY_MAY_RESTRICT` is deliberately never
    "POLICY_BLOCKS": see docs/NETWORK_DIAGNOSTICS.md Section 2/11 for why
    that distinction is load-bearing on this project's own lab cluster."""

    HEALTHY = "HEALTHY"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    MISSING_OR_UNREADY_ENDPOINTS = "MISSING_OR_UNREADY_ENDPOINTS"
    DNS_UNHEALTHY = "DNS_UNHEALTHY"
    POLICY_MAY_RESTRICT = "POLICY_MAY_RESTRICT"
    INCONCLUSIVE = "INCONCLUSIVE"


class PortMatchResult(BaseModel):
    service_port_name: str | None = None
    service_port: int
    target_port: str  # always stringified -- a numeric or a named targetPort
    matched_container_port: int | None = None
    matched: bool
    reasoning: str


class ServiceMatchResult(BaseModel):
    service_name: str
    selector: dict[str, str] = Field(default_factory=dict)
    matched_pod_names: list[str] = Field(default_factory=list)
    ready_pod_names: list[str] = Field(default_factory=list)
    endpointslice_endpoint_count: int = 0
    endpointslice_ready_count: int = 0
    #: True when the independently-computed selector match (above) agrees
    #: with the real EndpointSlice controller's own output -- see
    #: docs/NETWORK_DIAGNOSTICS.md Section 2 for why disagreement is itself
    #: a real, distinct finding, not noise.
    endpointslice_consistent: bool = True
    port_results: list[PortMatchResult] = Field(default_factory=list)


class NetworkPolicyRelevance(BaseModel):
    policy_name: str
    applies_to_target: bool
    policy_types: list[str] = Field(default_factory=list)
    summary: str


class DnsHealthResult(BaseModel):
    coredns_ready_replicas: int | None = None
    coredns_desired_replicas: int | None = None
    kube_dns_service_exists: bool = False
    kube_dns_endpoint_count: int = 0
    healthy: bool = False
    reasoning: str = ""


class NetworkFinding(BaseModel):
    finding_id: str
    namespace: str
    target_pod_name: str | None = None
    workload_kind: str | None = None
    workload_name: str | None = None
    analyzed_at: datetime
    dns_health: DnsHealthResult
    service_results: list[ServiceMatchResult] = Field(default_factory=list)
    applicable_network_policies: list[NetworkPolicyRelevance] = Field(default_factory=list)
    classification: NetworkFindingClassification
    reasoning: str
    related_incident_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

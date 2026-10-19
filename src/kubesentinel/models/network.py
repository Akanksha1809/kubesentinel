"""Network troubleshooting engine models: Service/EndpointSlice/port facts,
NetworkPolicy relevance, DNS health, the overall finding for one target
Pod's inbound connectivity story, and the active connectivity probe's own
result.

See docs/NETWORK_DIAGNOSTICS.md Section 2 for exactly what's evaluated
for real via static analysis (Service selector matching, EndpointSlice
consistency, port resolution, NetworkPolicy static analysis, CoreDNS
health) and Section 12 for the active probe (`ConnectivityProbeResult`,
below) that closes the one gap static analysis cannot: whether a packet
actually arrives and gets a response. Ingress/Gateway remain not
applicable on this lab cluster today.
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
    #: Set only for `NodePort`/`LoadBalancer`-type Services (`ServicePort.nodePort`,
    #: auto-allocated by the API server) -- `None` for `ClusterIP`/`ExternalName`,
    #: which have no node-level port at all. Purely informational: there is no
    #: real-world failure mode for this specific field this engine's own
    #: evidence can detect (the API server either allocates it successfully
    #: or the Service is never created), unlike `LoadBalancer`'s own external
    #: IP assignment below, which genuinely can and does get stuck.
    node_port: int | None = None


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
    #: `Service.spec.type`, always populated ("ClusterIP" is Kubernetes'
    #: own default when the field is unset, never left as `None` here).
    #: Every check above (selector match, EndpointSlice consistency, port
    #: resolution) is identical across all four types -- `NodePort`/
    #: `LoadBalancer` are strict supersets of `ClusterIP` routing, not a
    #: different code path -- so this is additive visibility, not a
    #: reason to branch the existing checks.
    service_type: str = "ClusterIP"
    #: Real `status.loadBalancer.ingress[].ip`/`.hostname` values, only
    #: ever populated for `service_type == "LoadBalancer"`. An empty list
    #: on a `LoadBalancer` Service is a genuine, common real-world state
    #: (no cloud controller present, or a provisioning failure/delay) --
    #: surfaced in `NetworkFinding.reasoning` as informational evidence,
    #: never as a classification change: a pending external IP says
    #: nothing about whether in-cluster Service routing to the target Pod
    #: (what this engine's classification actually answers) is working.
    load_balancer_ingress: list[str] = Field(default_factory=list)


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


class ConnectivityProbeResult(BaseModel):
    """The active connectivity probe's own result (docs/NETWORK_DIAGNOSTICS.md
    Section 12) -- genuinely dynamic evidence (a real DNS resolution, a
    real TCP connect attempt), unlike every other model in this file,
    which is static analysis of already-fetched Kubernetes objects.
    Deliberately a separate, standalone record, never a mutation of a
    past `NetworkFinding` -- `related_finding_id` is an optional,
    ID-only link, the same pattern `related_incident_ids` already uses
    elsewhere in this project."""

    probe_id: str
    namespace: str
    service_name: str
    port: int
    #: None means "not attempted" -- the Job never ran, or timed out
    #: before any marker was observed.
    dns_resolved: bool | None = None
    tcp_connected: bool | None = None
    #: False for a headless Service (no single ClusterIP to test) -- see
    #: Section 12.1. `succeeded` never considers tcp_connected when this
    #: is False.
    tcp_applicable: bool = True
    succeeded: bool = False
    raw_log: str = ""
    error: str | None = None
    related_finding_id: str | None = None
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

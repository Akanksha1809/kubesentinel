# Network Troubleshooting Engine: design

Milestone 4 of `docs/PLATFORM_ROADMAP.md`. Written before any `network/`
code exists, matching the precedent `RESOURCE_OPTIMIZATION_DESIGN.md` and
`SCHEDULING_ANALYSIS.md` both set. See `docs/IMPLEMENTATION_STATUS.md`'s
Capability D section for what already existed to build on.

## 1. What already exists, reused here

- `observer/inspector.py::endpointslice_evidence()` -- extracts ready/total
  endpoint counts from one EndpointSlice dict. Reused for the "how many of
  a Service's endpoints are ready" fact; extended (not duplicated) because
  it currently discards the slice's `ports` and each endpoint's `targetRef`
  -- both needed here and not elsewhere yet.
- `diagnostics/rules/service.py::ServiceSelectorMismatchRule` -- today's
  only Service-related check, and deliberately narrow: it only ever looks
  at EndpointSlice endpoint *counts*, never reads a `Service` object
  directly or compares its selector against real Pod labels. Kept exactly
  as-is (still a real, HIGH-value, zero-cost first signal); this engine
  adds the independent reconstruction underneath it, the same relationship
  `scheduling/` has with `diagnostics/rules/scheduling.py`.
- `models/evidence.py::EvidenceKind.SERVICE_SPEC` -- declared in the enum
  since Milestone 1, never produced by any code. The same "reserved but
  unused" shape `ReasonCode.DNS_RESOLUTION_FAILURE` already was before this
  milestone -- found by the same grep-for-unused-enum-values check
  `IMPLEMENTATION_STATUS.md`'s audit used elsewhere, not a coincidence.
  This milestone finally gives both a producer.
- `observer/watchers.py::_ListWatchLoop`'s raw-JSON-fetch technique
  (`list_fn(_preload_content=False, ...)` + `json.loads(...)`) -- reused by
  a plain one-shot helper (not the full watch loop; `network/` takes a
  snapshot per `analyze()` call, the same shape `optimization/`/`scheduling/`
  already use) for any EndpointSlice LIST call, since the same generated-
  client bug (`endpoints: null` on a genuinely empty slice) applies to a
  one-shot LIST exactly as much as to the continuous watcher.
- `optimization/ownership.py::resolve_pod_owner` -- reused for naming the
  owning workload behind a Service's matched Pods, the same way
  `scheduling/service.py` already reuses it.

## 2. Scope: what this milestone evaluates for real vs. defers

**Evaluated for real** (real cluster reads, real cross-referencing, never a
guess):

- **Service -> Pod selector matching.** `Service.spec.selector` compared
  directly against every Pod's own labels in the namespace -- not inferred
  from endpoint counts the way `ServiceSelectorMismatchRule` does today.
  Reports exactly which Pods match, and separately, how many of those are
  Ready.
- **EndpointSlice -> selector-match consistency.** Cross-checks the
  EndpointSlice controller's own actual endpoints against the independently
  computed selector-match set above. These *should* always agree (the
  EndpointSlice controller's whole job is to keep them in sync) --
  disagreement is itself a real, distinct, worth-surfacing finding (controller
  lag, or a bug), not something either existing check can currently see.
- **Port / targetPort matching.** Each `Service.spec.ports[].targetPort`
  (by name or number) resolved against the matched Pods' real container
  ports. A named `targetPort` that doesn't match any container port by name
  is a real, concrete, previously-undetectable misconfiguration.
- **NetworkPolicy static analysis.** Every `NetworkPolicy` in the
  namespace whose `podSelector` matches the target Pod, its
  `policyTypes`/`ingress`/`egress` rules, and whether a given peer
  (namespace/pod selector or IP block) and port would, as written, permit
  or restrict a specific flow. Always phrased as "this policy's rules would
  do X" -- never "traffic is blocked," per the environment constraint
  below.
- **CoreDNS health.** `coredns` Deployment readiness and `kube-dns`
  Service/EndpointSlice health in `kube-system` -- a new, narrow,
  explicitly-justified RBAC exception (Section 7).
- **The request-path narrative.** `Client/Pod -> DNS -> Service ->
  EndpointSlice -> destination Pod`, built entirely from the facts above,
  each step's evidence quality stated, with an explicit, un-skippable
  closing caveat: Kubernetes API inspection cannot prove the packet
  actually arrived. "Inconclusive -- needs an active test to confirm" is a
  legitimate, honest terminal state for this engine, not a failure to
  reach a conclusion.

**Deliberately deferred, not built this milestone** (named explicitly,
matching the exact "detected but not evaluated, a boundary not an
oversight" discipline `scheduling/`'s pod-affinity/topology-spread handling
already established):

- **The active DNS/connectivity probe** the spec describes ("a bounded,
  explicitly enabled diagnostic mechanism in the dedicated lab namespace").
  This would be the first mutation capability in this project outside the
  fully policy-gated `RemediationExecutor` -- creating even a short-lived,
  auto-cleaned-up diagnostic Job is a genuinely different risk class from
  every other read-only analysis engine built so far, and deserves its own
  focused design and safety review, not a late addition folded into an
  already-large static-analysis milestone. The static analysis above is
  already honest about its own limit ("cannot prove end-to-end
  connectivity") rather than silently pretending the active probe isn't
  needed -- this is the spec's own anticipated "inconclusive, needs further
  testing" case, used as designed, not as a excuse to skip the harder work.
- **Ingress/Gateway resources.** The spec's own instruction is conditional
  ("if the existing project supports them"). Confirmed live: zero
  `IngressClass` objects, zero Gateway API CRDs, and no existing
  Ingress/Gateway manifests anywhere in this repository's history. Nothing
  to analyze -- not a gap, an honest "not applicable."

## 3. Module layout

```
src/kubesentinel/network/
  __init__.py
  service_inventory.py   # Services + their Pod/EndpointSlice/port facts,
                          # one-shot snapshot per analyze() call
  policy_inventory.py     # NetworkPolicy objects + which apply to a target Pod
  dns_health.py            # CoreDNS Deployment/Service/EndpointSlice health
  analyzer.py               # stitches the above into the request-path
                             # narrative + a NetworkFinding
  service.py                 # NetworkService: the one module with side
                              # effects, mirrors SchedulingService exactly
```

`models/network.py`, `persistence/network_repo.py` (a fourth sibling
behind `persistence/interface.py`), `api/network_routes.py` (a new
`APIRouter`), new CLI commands -- the same pattern
`MASTER_ARCHITECTURE.md` describes, now proven a fourth time.

## 4. Data model

```python
class PortMatchResult(BaseModel):
    service_port_name: str | None
    service_port: int
    target_port: str | int
    matched_container_port: int | None   # None if targetPort resolves to nothing real
    matched: bool
    reasoning: str

class NetworkPolicyRelevance(BaseModel):
    policy_name: str
    applies_to_target: bool    # does its podSelector match the target Pod
    policy_types: list[str]    # ["Ingress"], ["Egress"], or both
    # Static description only -- see Section 2. Never a claim about
    # observed enforcement.
    summary: str

class ServiceMatchResult(BaseModel):
    service_name: str
    selector: dict[str, str]
    matched_pod_names: list[str]
    ready_pod_names: list[str]
    endpointslice_endpoint_count: int
    endpointslice_ready_count: int
    # True if the independently-computed selector match and the real
    # EndpointSlice controller output agree -- see Section 2.
    endpointslice_consistent: bool
    port_results: list[PortMatchResult]

class DnsHealthResult(BaseModel):
    coredns_ready_replicas: int
    coredns_desired_replicas: int
    kube_dns_service_exists: bool
    kube_dns_endpoint_count: int
    healthy: bool
    reasoning: str

class NetworkFinding(BaseModel):
    finding_id: str
    namespace: str
    target_pod_name: str | None   # the Pod whose connectivity is being explained
    workload_kind: str | None
    workload_name: str | None
    analyzed_at: datetime
    dns_health: DnsHealthResult
    service_results: list[ServiceMatchResult]
    applicable_network_policies: list[NetworkPolicyRelevance]
    # The request-path narrative, as a classification, not a connectivity proof:
    classification: NetworkFindingClassification
    reasoning: str
    related_incident_ids: list[str] = []   # same by-ID pattern, same
                                            # dual-UID lookup as scheduling/optimization
    created_at: datetime

class NetworkFindingClassification(str, Enum):
    HEALTHY = "HEALTHY"                       # every checked step looks correct
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"   # selector/port mismatch, etc.
    MISSING_OR_UNREADY_ENDPOINTS = "MISSING_OR_UNREADY_ENDPOINTS"
    DNS_UNHEALTHY = "DNS_UNHEALTHY"
    POLICY_MAY_RESTRICT = "POLICY_MAY_RESTRICT"   # never "POLICY_BLOCKS" -- see Section 2
    INCONCLUSIVE = "INCONCLUSIVE"              # static evidence genuinely insufficient
```

No `proposed_*`/apply fields, same reasoning as `scheduling/`: this engine
explains, never changes anything.

## 5. API and CLI

Same flat-path convention, no `/api/v1` prefix:

| Route | Behavior |
|---|---|
| `POST /network/analyze` | The only route that touches the cluster. Body: `{"namespace": str, "pod_name": str}` -- analyzes the given Pod's inbound connectivity story (which Services select it, whether its ports match, which policies apply, DNS health). Persists and returns a `NetworkFinding`. |
| `GET /network/findings` | Reads persisted findings, filterable by `namespace`/`classification`. |
| `GET /network/findings/{id}` | One finding in full. 404 if missing. |

CLI: `kubesentinel network-check <namespace> <pod-name>` and
`kubesentinel network-findings`, mirroring `schedule-check`/
`scheduling-findings`'s own naming shape.

## 6. Safety

Entirely read-only, the same guarantee every other engine in this project
proves the same way: no `ClusterClients` method outside `list_*`/`get_*`
anywhere in `network/`, checked with the same HTTP-transport-level live
test the other two engines already established. No apply path exists or is
implied. The deferred active probe (Section 2) is exactly why this matters
to say explicitly now: this milestone adds zero new mutation surface to
the project, full stop.

## 7. RBAC

Two new, separate, narrowly-scoped Roles (never folded into an existing
one, matching every prior capability's own precedent):

- `rbac/network-role.yaml` (namespace: `kubesentinel-lab`): `services`
  (get/list -- the one genuinely new core-API grant; nothing has ever
  listed Services before this milestone) and `networkpolicies.networking.k8s.io`
  (get/list). `pods`/`deployments`/`endpointslices` are already granted by
  existing Roles, reused rather than re-granted.
- `rbac/network-dns-role.yaml` (namespace: `kube-system`): `deployments`
  (get/list, scoped by name to `coredns` where the API allows it, or
  documented as a namespace-wide read if resourceNames isn't practical for
  list calls), `services` + `endpointslices.discovery.k8s.io` (get/list).
  **A deliberate, narrow exception to this project's "lab namespace only"
  RBAC boundary**, the same category of exception `clusterrole.yaml`'s own
  Node grant already is -- CoreDNS doesn't run in the lab namespace, so
  checking its health genuinely needs a `kube-system`-scoped grant; kept to
  exactly the three resources this check reads, no Secrets, no write
  verbs, no blanket `kube-system` access.

## 8. Persistence

A new `network_findings` table in the same SQLite file, via a new
`SQLiteNetworkRepository` behind `persistence/interface.py` -- same pattern
as the other two repositories.

## 9. Observability

`kubesentinel_network_analysis_total{result}` (classification value,
collapsed to the same bounded label set as `NetworkFindingClassification`),
matching the existing `_analysis_total{result}` shape both other engines
already use.

## 10. Testing strategy

- **Unit**: service/port/policy matching functions tested against
  hand-built `V1Service`/`V1Pod`/`V1NetworkPolicy` objects -- selector
  match/mismatch, named vs. numeric targetPort resolution, a policy whose
  podSelector does/doesn't match the target, EndpointSlice-vs-selector
  consistency and (deliberately constructed) inconsistency, DNS health
  healthy/degraded. Determinism, matching every other analyzer.
- **Integration**: the read-only HTTP-transport check; API route
  validation/error codes.
- **Live**: the existing `scenario-service-selector-mismatch` (zero
  endpoints, already live-verified by the troubleshooting engine, now also
  independently confirmed by this engine's own selector-vs-Pod-labels
  math) plus a new, dedicated scenario exercising a port/targetPort
  mismatch (a selector that matches real, Ready Pods, but a `targetPort`
  naming a container port that doesn't exist) -- not currently demonstrated
  by any existing scenario, and specifically chosen because it's a failure
  mode none of the existing diagnostic rules can see at all (a Service with
  real, ready endpoints can still misroute every request if its
  `targetPort` is wrong).

## 11. Known limitations, stated once here rather than per-finding

kindnet (this cluster's default CNI) does not enforce NetworkPolicy --
confirmed in Milestone 20's audit, repeated here because it's load-bearing
for this entire capability: every `NetworkPolicy`-related finding is
static analysis of the policy object only, worded as "would restrict" or
"applies to," never "blocks" or "prevents." A CNI swap (e.g. Calico) would
be the only way to make an enforcement claim honestly on this lab cluster,
and is out of scope unless explicitly requested.

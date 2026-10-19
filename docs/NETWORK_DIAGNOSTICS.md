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
  **Closed -- see Section 12.**
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

`analyze_pod`/`service_inventory.py`/`policy_inventory.py`/`dns_health.py`
are entirely read-only, the same guarantee every other engine in this
project proves the same way: no `ClusterClients` method outside
`list_*`/`get_*` anywhere in those modules, checked with the same
HTTP-transport-level live test the other two engines already established.
This milestone's own original scope added zero new mutation surface to
the project, full stop. `network/probe.py` (Section 12) is the one
deliberate, later exception -- its own dedicated safety section covers it
in full; this section's read-only guarantee continues to hold for every
module named above, unchanged.

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

## 12. The active connectivity probe

Closes the gap Section 2 deferred and Section 6 promised its own review.
The fourth and fifth of the five scope boundaries requested together were
explicitly sequenced last, by the user's own choice, for exactly the
reason Section 2 already gave: this is this project's first new mutation
capability outside the fully policy-gated `RemediationExecutor`, and
deserves a safety review as careful as that one originally got, not a
smaller version of it.

### 12.1 What it tests, and why those two checks specifically

The request-path narrative this engine already builds
(`Client -> DNS -> Service -> EndpointSlice -> destination Pod`) names
exactly where static analysis runs out: it can prove a Service's selector
matches Ready Pods and that a `targetPort` resolves, but never that a
packet sent to it actually arrives and gets a response. The probe closes
that specific, named gap with two independent checks against one target
`(namespace, service_name, port)`, run from inside a throwaway Job in the
*same* namespace as the target Service (the closest available
approximation of "a client Pod in that namespace trying to reach it"):

1. **DNS resolution** of the Service's own full in-namespace FQDN
   (`nslookup <service_name>.<namespace>.svc.<cluster_domain>`) --
   confirms CoreDNS genuinely answers for this specific Service today,
   not just that the `coredns` Deployment is generically Ready (Section
   2's existing DNS health check). **Deliberately the FQDN, not the bare
   short name a Pod in the same namespace could also use** -- caught
   live: BusyBox's `nslookup`, resolving a bare name through
   `resolv.conf`'s search-domain list (`ndots:5`, several search
   suffixes tried in turn), returns a **nonzero exit code even when a
   later search domain resolves successfully** -- confirmed directly via
   `kubectl exec` against this project's own lab cluster, comparing a
   bare-name lookup (exit 1, despite printing a correct final address)
   against the same Service's FQDN (exit 0, one clean attempt, same
   address). The script's own `nslookup ... && echo ..._OK` marker
   depends on a reliable exit code, so the bare short name would have
   silently misreported a real success as a DNS failure -- this was
   caught by live verification, not unit tests (which mock the log
   entirely), and fixed before this section's own first draft was
   final.
2. **TCP connect to the Service's real `ClusterIP:port`** (`nc -z -w
   <timeout> <cluster_ip> <port>`) -- deliberately the ClusterIP, read
   fresh via `read_namespaced_service` (one new, small, read-only API
   call), not the DNS name, so this check is genuinely independent of
   check 1: a cluster where DNS is broken but kube-proxy/the Service/its
   endpoints are healthy should still show DNS failing and TCP
   succeeding, not one undifferentiated failure. This is the check that
   actually answers "did the packet arrive and get a response" --
   everything upstream of it (selector match, port resolution,
   EndpointSlice consistency) is Section 2's static analysis; this is the
   one dynamic fact none of it can produce.

**Deliberately bounded to exactly these two checks, not a general-purpose
network debugging tool.** The probe takes one target port, never scans a
range; it never execs into an existing, arbitrary user Pod (every
command this feature ever runs is the one fixed script below, in a
throwaway container this feature creates and owns, never someone else's
workload); and it makes no claim beyond "DNS resolved or didn't, TCP
connected or didn't" -- no traceroute, no packet capture, no latency
claims. A **headless Service** (`spec.clusterIP in (None, "", "None")`)
has no single ClusterIP to test; the probe runs the DNS check only and
reports the TCP check as not-applicable, named explicitly rather than
guessed at or silently skipped.

### 12.2 The probe mechanism, and the safety properties it's built to hold

A new `BatchV1Api.create_namespaced_job` call (the `batch` client already
exists on `ClusterClients`, added for Milestone 2's own read-only Job
listing -- this is the first *write* use of it anywhere in this project)
creates one Job per probe request, in the target Service's own namespace,
running one fixed container:

```yaml
# Illustrative -- see network/probe.py for the real, typed construction.
apiVersion: batch/v1
kind: Job
metadata:
  name: kubesentinel-probe-<short-uuid>
  labels:
    kubesentinel.io/probe: "true"
spec:
  activeDeadlineSeconds: 15        # network_probe_job_timeout_seconds
  backoffLimit: 0                   # never retries -- one attempt, one result
  ttlSecondsAfterFinished: 60       # network_probe_job_ttl_seconds; backstop
                                     # cleanup if explicit deletion (below) fails
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false   # the probe needs no API access at all
      containers:
        - name: probe
          image: busybox:1.36       # pinned, never :latest -- this Job must
                                     # never trip its own security auditor's
                                     # MUTABLE_IMAGE_TAG check (12.4)
          command: ["sh", "-c", <fixed script, Section 12.3>]
          env:                       # target data flows ONLY through typed env
            - {name: KUBESENTINEL_PROBE_HOST, value: <service's full in-namespace FQDN, 12.1>}
            - {name: KUBESENTINEL_PROBE_CLUSTER_IP, value: <Service clusterIP>}
            - {name: KUBESENTINEL_PROBE_PORT, value: <str(port)>}
            - {name: KUBESENTINEL_PROBE_TIMEOUT, value: <str(timeout)>}
          securityContext:
            allowPrivilegeEscalation: false
            privileged: false
            runAsNonRoot: true
            runAsUser: 65534          # "nobody" -- busybox:1.36 defaults to
                                       # root; runAsNonRoot alone, without
                                       # this, makes the kubelet refuse to
                                       # start the container at all (caught
                                       # live: CreateContainerConfigError,
                                       # which looked identical to a hung
                                       # Job from the outside until
                                       # `kubectl describe pod` named it)
            readOnlyRootFilesystem: true
```

Every property below is a deliberate safety invariant, not an
afterthought, and every one is covered by a dedicated unit test (12.6):

1. **Off by default, whole feature.** `Settings.network_probe_enabled`
   (default `False`) gates the entire capability -- `False` means
   `network/probe.py` is never imported into a code path that runs, zero
   new API calls happen, identical to this section not existing, the
   same "genuinely optional" guarantee `security_enable_image_scanning`
   (docs/SECURITY_AUDITING.md Section 14) already established for this
   project's other opt-in extras.
2. **The container's command is a fixed, hardcoded string, never built
   from caller input.** Every piece of caller-supplied data (service
   name, ClusterIP, port, timeout) flows into the container exclusively
   through typed `env` entries on a `V1EnvVar` list -- never through
   Python string formatting into the shell command itself. The script
   references `"$KUBESENTINEL_PROBE_HOST"` etc. as quoted shell
   variables; this is what makes injection structurally impossible
   regardless of what a caller passes, not merely unlikely -- a
   dedicated test asserts the constructed command string is identical
   across wildly different (and deliberately adversarial-looking) inputs
   (12.6).
3. **Namespace allowlist**, mirroring remediation's own
   `action_allowed_namespaces` in shape but kept as its *own*, separate
   setting (`network_probe_allowed_namespaces`, default
   `"kubesentinel-lab"`) -- conflating this with the remediation
   allowlist would wrongly imply the two features share a risk model;
   they don't (one patches/rolls back/scales a Deployment, this creates
   and deletes a throwaway Job). A probe request for a namespace not on
   this list is refused before any cluster call is made.
4. **Rate-limited.** `network_probe_cooldown_seconds` (default 30,
   per `(namespace, service_name, port)`) and
   `network_probe_max_concurrent_per_namespace` (default 1, counted by
   listing Jobs carrying the `kubesentinel.io/probe=true` label in the
   target namespace that haven't finished yet) -- a small, dedicated gate
   purpose-built for this feature, not routed through `PolicyEngine`/
   `ActionType`/`RemediationAction`, which model "mutate an existing
   Deployment" and don't fit "create and delete a throwaway diagnostic
   Job" at all. `policy_mode` (OBSERVE/APPROVAL_REQUIRED/CONTROLLED_AUTO)
   is deliberately **not** consulted here either: it's specifically
   documented as gating *remediation* actions, and a connectivity probe
   fixes nothing -- overloading it to also mean "no diagnostic Jobs
   either" would quietly change what that setting means for every
   existing deployment already relying on its documented behavior.
5. **Always time-bounded, three ways.** `activeDeadlineSeconds` (Job-level,
   `network_probe_job_timeout_seconds`, default 15s) ensures the Job
   cannot run forever even if the script hangs; `ttlSecondsAfterFinished`
   (`network_probe_job_ttl_seconds`, default 60s) is a backstop that
   garbage-collects the Job and its Pod even if this project's own
   explicit deletion (next point) never runs at all -- e.g. the process
   crashes mid-probe. **A third, live-caught bound**:
   `network_probe_api_timeout_seconds` (default 10s, matching
   `RemediationExecutor`'s own `action_execution_timeout_seconds`
   precedent) is passed as `_request_timeout` on *every* Kubernetes API
   call `run_probe`/`network/probe.py` make (`read_namespaced_service`,
   create/read-status/delete on the Job, list/read-log on its Pod).
   Without it, `wait_for_job_completion`'s own outer deadline is only
   checked *between* polling calls, never during one -- a single slow or
   hung apiserver response could block the whole probe far longer than
   any of the timeouts above implied. Caught live, not in a unit test
   (which mocks every client call): against a genuinely overloaded local
   cluster, one probe attempt took over 20 minutes to return before this
   fix, nowhere near the ~20-30s every other timeout here promises.
   Never unbounded, under any failure mode -- now including a slow
   server, not just a hung script.
6. **Explicit cleanup, backstopped, not relied upon alone.**
   `network/probe.py` deletes the Job (with `propagation_policy="Background"`
   so its Pod is garbage-collected too) in a `finally`-equivalent after
   reading its result, whether the probe succeeded, failed, or the
   service's own wait for completion (`network_probe_poll_timeout_seconds`,
   default 20s, polled every `network_probe_poll_interval_seconds`,
   default 1s) itself timed out. If that delete call *also* fails (e.g. a
   transient API error), point 5's TTL still cleans it up -- defense in
   depth, not a single point of failure for cleanup.
7. **No cluster credentials inside the probe container.**
   `automountServiceAccountToken: false` -- the probe needs no
   Kubernetes API access at all to do its job (DNS + a raw TCP connect),
   so it gets none, closing off an entire class of "what if this
   throwaway container were compromised" concern before it's even worth
   asking.
8. **The probe's own Pod template passes every existing container-security
   check this project already has**, by construction, not by luck:
   non-root, no privilege escalation, read-only root filesystem, no host
   namespaces, no hostPath volumes, a pinned (never `:latest`) image, no
   Secrets as env vars. Live-verified directly (12.7): running
   `kubesentinel security-audit` against the lab namespace while a probe
   Job is in flight produces zero findings naming it.

### 12.3 The fixed probe script

```sh
set -e
echo "KUBESENTINEL_PROBE_DNS_ATTEMPT"
nslookup "$KUBESENTINEL_PROBE_HOST" && echo "KUBESENTINEL_PROBE_DNS_OK"
if [ -n "$KUBESENTINEL_PROBE_CLUSTER_IP" ]; then
  echo "KUBESENTINEL_PROBE_TCP_ATTEMPT"
  nc -z -w "$KUBESENTINEL_PROBE_TIMEOUT" "$KUBESENTINEL_PROBE_CLUSTER_IP" "$KUBESENTINEL_PROBE_PORT" && echo "KUBESENTINEL_PROBE_TCP_OK"
fi
```

`set -e` means a failing `nslookup` exits before the TCP section ever
runs -- deliberate: if DNS is broken, `KUBESENTINEL_PROBE_CLUSTER_IP` was
still resolved by *this project's own* `read_namespaced_service` call
(never by the probe's own DNS), so the TCP check's independence from DNS
is preserved regardless -- but a pipeline that already failed its first
step exiting early, rather than continuing to probe-and-ignore, matches
`set -e`'s own standard meaning and keeps the script's own logic legible
rather than adding bespoke continue-past-failure handling for a case
(DNS literally broken cluster-wide) that `dns_health.py` already reports
on its own, independently. `network/probe.py` reads the Job's Pod's logs
(`read_namespaced_pod_log`, one bounded read) and classifies the result
from exactly which `KUBESENTINEL_PROBE_*_OK` markers are present --
never by parsing `nslookup`/`nc`'s own free-text output, which differs
across BusyBox versions and is not a contract this project should depend
on.

### 12.4 Data model

```python
class ConnectivityProbeResult(BaseModel):
    probe_id: str
    namespace: str
    service_name: str
    port: int
    dns_resolved: bool | None       # None: not attempted (job never ran/timed out)
    tcp_connected: bool | None      # None: not attempted -- includes the
                                     # headless-Service case (12.1) and the
                                     # set-e-short-circuit-after-DNS-failure case
    tcp_applicable: bool             # False for a headless Service (12.1)
    succeeded: bool                  # dns_resolved and (tcp_connected or not tcp_applicable)
    raw_log: str                     # the Job Pod's own stdout, bounded/truncated
    error: str | None = None          # e.g. "timed out waiting for Job completion"
    #: Same ID-only link pattern as SchedulingFinding.related_incident_ids --
    #: optional, set only when a caller explicitly ties this probe to the
    #: NetworkFinding it's trying to confirm/refute. Never auto-populated,
    #: and running a probe never mutates a past NetworkFinding -- findings
    #: stay exactly what their own analyze() call produced.
    related_finding_id: str | None = None
    requested_at: datetime
    completed_at: datetime | None = None
```

### 12.5 API, CLI, persistence, RBAC, observability

- `POST /network/probe` -- body `{"namespace": str, "service_name": str,
  "port": int, "related_finding_id": str | None}`. Returns 403 (not 503 --
  a deliberate, distinct code) if `network_probe_enabled` is `False` or
  the namespace fails the allowlist, so a caller can tell "this feature
  is off" apart from "the cluster is unreachable." `GET
  /network/probe-results` / `GET /network/probe-results/{id}` mirror
  `findings`'s own list/get shape.
- CLI: `kubesentinel network-probe <namespace> <service> <port>`, mirroring
  `network-check`'s own shape; prints which of DNS/TCP succeeded plainly.
- `SQLiteNetworkRepository` gains a second table,
  `connectivity_probe_results`, same one-row-per-result/JSON-blob shape
  `network_findings` already uses -- a sibling table in the same file,
  not a new repository class (same capability, same package).
- `rbac/network-probe-role.yaml` -- a new, separate Role (never folded
  into `network-role.yaml`, same independent-auditability precedent every
  prior capability's own Role already follows): `batch/v1` `jobs`
  (create, get, list, delete) and `pods`/`pods/log` (get, list), lab
  namespace only. The clearest-yet example in this project of "a Role
  whose very existence signals elevated risk" -- reviewers checking this
  project's blast radius should be able to find every write-capable Role
  by filename alone, and this one's name says exactly what it is.
- `kubesentinel_network_probe_total{result}` (dns_ok_tcp_ok /
  dns_ok_tcp_failed / dns_failed / timeout / denied / failed), mirroring
  every other `_total{result}` counter's shape.

### 12.6 Testing strategy

- **Unit**: the fixed-script-construction test (12.2 point 2) with
  deliberately adversarial inputs (`service_name` containing shell
  metacharacters that would be dangerous if ever string-formatted --
  confirming the command is unaffected because those values only ever
  reach the container via `env`, never the script text); namespace
  allowlist allow/deny; cooldown and max-concurrent gating; headless-
  Service handling (`tcp_applicable=False`, DNS-only); log-marker
  classification for every combination (DNS ok/TCP ok, DNS ok/TCP failed,
  DNS failed entirely, no markers at all e.g. image pull failure); the
  Job spec's own security properties (non-root, no privilege escalation,
  pinned image, no SA token) asserted directly against the constructed
  `V1Job` object, not just against a live cluster's after-the-fact
  behavior.
- **Integration**: a mocked-`ClusterClients` test confirming
  `create_namespaced_job` and `delete_namespaced_job` are each called
  exactly once per probe, including on every one of the failure paths
  (Job never completes in time, log read fails) -- cleanup must never be
  skipped.
- **Live**: with `network_probe_enabled=true`, probe a real, healthy
  Service in the lab namespace and confirm a real `dns_resolved=True,
  tcp_connected=True` result; probe a Service whose real Pods are
  deliberately scaled to zero (or a port nothing listens on) and confirm
  a real `tcp_connected=False`; confirm live, via `kubectl get jobs,pods`,
  that nothing from either probe remains in the cluster seconds after
  each call returns; confirm live, via `kubesentinel security-audit`,
  that a probe Job in flight produces zero container-security findings
  naming it (12.2 point 8).

### 12.7 Known limitations, stated once here

No UDP support (`nc -z` as used here is TCP-only -- a real, simple v1
boundary, not an oversight: the project's own Services so far are all
TCP). No support for probing a Pod directly (only a Service, matching
this capability's own existing "inbound Service-based connectivity"
framing throughout Sections 2-11) -- probing arbitrary Pod-to-Pod
connectivity would be a different, broader feature. Only one port per
probe call, by design (12.1) -- not a technical limitation, a deliberate
scope fence against this becoming a general port-scanning tool.

## 13. Service type awareness (closing the "Service types / routing: Partial" gap)

`docs/IMPLEMENTATION_STATUS.md` named this capability's own narrowest
spot: "ClusterIP routing... is the implemented path;
`NodePort`/`LoadBalancer`/`ExternalName` type-specific routing is not
separately modeled." Closed here, additively.

### 13.1 Why no new routing logic was needed

`NodePort` and `LoadBalancer` are not alternate routing paths this
engine needs to re-derive -- they are strict supersets of `ClusterIP`.
Every Service of either type still gets a `ClusterIP`, still gets
selected the same way, still gets the same `EndpointSlice`, still
resolves `targetPort` the same way. The *only* genuinely new state is
each type's own extra piece: `NodePort`'s auto-allocated node-level
port (`ServicePort.nodePort`), and `LoadBalancer`'s external address
assignment (`status.loadBalancer.ingress`). Both are therefore additive
fields on the existing `PortMatchResult`/`ServiceMatchResult` models
(`service_type`, `PortMatchResult.node_port`,
`ServiceMatchResult.load_balancer_ingress`), not a parallel analysis
path -- `service_inventory.py::_match_one_service` reads three more
fields off the same already-fetched `V1Service` object it already had.

`ExternalName` needed no new handling at all: it has no selector and no
backend Pods, so it already, correctly, never appears in
`selecting_services` for any target Pod -- this capability's own
existing "no Service selects this Pod" path already covers it, and
inventing a dedicated check for a Service type that structurally can
never route inbound traffic to a Pod would be exactly the kind of
scaffolding-ahead-of-need this project avoids elsewhere.

### 13.2 A pending LoadBalancer is informational, never a classification change

A `LoadBalancer` Service with no assigned external IP/hostname yet
(`status.loadBalancer.ingress` empty) is a genuinely common real-world
state -- no cloud controller present (this project's own kind lab, with
no MetalLB installed, has exactly this state permanently), a quota
limit, or a provisioning delay. It is appended to `NetworkFinding.reasoning`
as its own sentence, but it never changes `classification`: this
capability's classification answers "does in-cluster Service routing
to the target Pod work," and that question's answer is unaffected by
whether the Service's *external* address has been assigned yet -- the
Service still routes via its `ClusterIP` exactly like any other Service
in the meantime. Claiming a stronger connection between "LoadBalancer
pending" and "this Pod is unreachable" would be exactly the kind of
overclaim Section 2's own `POLICY_MAY_RESTRICT` precedent already
guards against for NetworkPolicy evidence.

### 13.3 Live-verified against the real cluster, not just unit-tested

Applied a real Deployment with a `LoadBalancer`-type Service and a
`NodePort`-type Service, both selecting it, to the actual
`kind-kubesentinel` cluster (kind's own default setup, no MetalLB or
any other cloud-LB controller installed). Confirmed via `kubectl get
svc` and `kubesentinel network-check` together: the `LoadBalancer`
Service's `EXTERNAL-IP` genuinely stayed `<pending>`, and the engine's
own `load_balancer_ingress` correctly read `[]` with the informational
reasoning sentence present, while `classification` stayed `HEALTHY`
(in-cluster routing to the Pod was never actually broken). The
`NodePort` Service's real, API-server-allocated `nodePort` (`32641` in
this run) was correctly read back via `PortMatchResult.node_port`,
matching `kubectl get svc`'s own output exactly.

## 14. Error-handling granularity, and joining the shared evidence-completeness vocabulary

`docs/EVIDENCE_COMPLETENESS.md` named this engine's own gap explicitly
when three other capabilities' models first adopted a shared
`EvidenceCompleteness`/`warnings` vocabulary: `analyze_pod` was
genuinely all-or-nothing -- `check_dns_health`/
`build_service_match_results`/`build_applicable_policies` were called
directly, no individual sub-read wrapped in its own `try`/`except`, so
any single failure aborted the whole analysis (one outer `except
Exception`, surfaced as a 503) rather than degrading. `NetworkFinding`
had nowhere to attach a partial-failure signal until that granularity
changed. Closed here, additively -- see `docs/EVIDENCE_COMPLETENESS.md`
§5 for the full design; this section covers what it means for this
engine specifically.

### 14.1 The fatal/degradable split applied to `analyze_pod`

The same judgment `scheduling/service.py` already applies to its own
ResourceQuota/LimitRange reads (admission-time-only, genuinely
optional) versus its Node/Pod reads (foundational, correctly fatal):

- **Stays fatal**: the target Pod read itself (`PodNotFoundError`), and
  `build_service_match_results` -- the core Service/Pod/EndpointSlice
  evidence this finding is actually *about*. Either failing leaves
  nothing meaningful to report, so `analyze_pod` still aborts (a 503,
  unchanged).
- **Now degradable**: ReplicaSet listing (used only for owner
  resolution, feeding `related_incident_ids`) and
  `build_applicable_policies` (NetworkPolicy evaluation). Each is now
  individually wrapped in `try`/`except ApiException`; on failure,
  `analyze_pod` degrades to `owner = None` / `applicable_policies = []`
  respectively, appends a reason to a local `warnings: list[str]`, and
  continues rather than aborting.

### 14.2 A second, more subtle instance of the same gap, found while wiring this up

`check_dns_health` already caught `ApiException` on each of its own
three sub-reads (the coredns Deployment, the kube-dns Service, its
EndpointSlices) and degraded to `DnsHealthResult(healthy=False, ...)`
internally -- it never propagated a failure up to `analyze_pod` at all.
But `healthy=False` alone conflates two different claims: "DNS is
confirmed down" and "the read itself failed, so I couldn't tell." A
403 reading the coredns Deployment produced a confident-looking
`DNS_UNHEALTHY` finding, indistinguishable from a genuine outage.

Fixed with a new `DnsHealthResult.check_succeeded: bool = True` field,
set to `False` on each of the three failure branches in
`network/dns_health.py` -- `healthy` itself is never touched, so the
pre-existing classification behavior and its own test suite are
unchanged. `analyze_pod` now checks this flag and, when `False`,
appends `dns_health.reasoning` to the same `warnings` list rather than
silently trusting a health verdict it didn't compute itself. The
`classification` still reports `DNS_UNHEALTHY` in that case -- this
function has no independent way to second-guess the result -- but the
finding's `warnings`/`evidence_completeness` now tell a caller the
difference between "DNS is actually down" and "I couldn't check."

### 14.3 `NetworkFinding` now carries the shared vocabulary

`models/network.py::NetworkFinding` now inherits
`EvidenceCompletenessMixin` (`warnings: list[str]`,
`evidence_completeness: EvidenceCompleteness`, both additive, no
existing field renamed or removed). `analyzer.py::analyze_network`
gained a `warnings: list[str] | None` parameter, threaded through from
`service.py::analyze_pod`'s own collected list; it sets
`evidence_completeness = DEGRADED if warnings else COMPLETE` on the
returned finding. `warnings` never changes `classification` -- the
same non-negotiable separation Section 13.2 already establishes for
the LoadBalancer-pending note: evidence completeness and the
diagnostic verdict are reported alongside each other, never folded
into one signal.

### 14.4 Live-verified against the real cluster

Ran `kubesentinel network-check` against a real throwaway Pod on the
actual `kind-kubesentinel` cluster with no RBAC denied: confirmed
`evidence_completeness: COMPLETE`, `warnings: []`, and
`dns_health.check_succeeded: true` for the no-failure path -- matching
the pre-existing, unaffected happy path exactly. The three new failure
paths (ReplicaSet listing denied, NetworkPolicy listing denied, a DNS
sub-read failing) are each covered by a unit test with a real
`ApiException(403)` rather than reproduced by revoking this project's
own real RBAC mid-session on a shared cluster -- the same deliberate
choice `docs/EVIDENCE_COMPLETENESS.md` §7 already documents for the
other three retrofitted models.

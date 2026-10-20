# Scheduling & Placement Advisor: design

Milestone 3 of `docs/PLATFORM_ROADMAP.md`. Written before any
`scheduling/` code exists, matching the precedent
`RESOURCE_OPTIMIZATION_DESIGN.md` set for Capability B. See
`docs/IMPLEMENTATION_STATUS.md`'s Capability C section for exactly what
already exists to build on and what doesn't.

## 1. What already exists, reused here

- `diagnostics/rules/scheduling.py::classify_unschedulable_message()` --
  the scheduler's own message, classified into a coarse reason code. Kept
  as the first, cheapest check (HIGH confidence, zero computation); this
  milestone adds the independent reconstruction underneath it, not a
  replacement for it.
- `optimization/quantities.py::parse_cpu_to_millicores`/
  `parse_memory_to_bytes` -- reused by import, not duplicated. The exact
  same `Decimal`-based parsing scheduling needs for Node allocatable and
  Pod requests is already correct and already tested (44 unit tests). This
  is a cross-package import (`scheduling/` importing from `optimization/`),
  the same kind of reuse `optimization/analyzer.py` already does importing
  from `diagnostics/`. No code moves; `quantities.py` stays where it is.
- `observer/client.py::ClusterClients.core` -- `core.list_node()` and
  `core.list_namespaced_resource_quota()` both already exist on the
  existing `CoreV1Api` client. **No new `ClusterClients` field needed for
  this milestone** -- the only thing missing is RBAC (below), not a client.
- Pod resource requests/limits parsing -- `optimization/inventory.py`
  already extracts these per-container. Not imported directly (scheduling
  needs *all* Pods on a Node, including ones optimization's own inventory
  pass may exclude for other reasons), but the same parsing helpers are
  reused, and the two inventories are built the same way deliberately.

## 2. Scope: what this milestone evaluates for real vs. detects-but-doesn't-simulate

Per the spec's own instruction ("explain and simulate scheduling behavior,
not implement a production scheduling algorithm") and "document
unsupported features and approximations" -- stated here precisely, not
left implicit:

**Evaluated for real** (the finding states pass/fail with the actual
reason, computed from the actual Node/Pod objects):

- **Resource fit**: Node `status.allocatable` (cpu/memory) vs. the sum of
  the *effective* request of every *other* Pod currently bound to that
  Node (the scheduler's own accounting unit) plus the candidate Pod's
  own effective request. "Effective" per Pod is
  `node_inventory.py::effective_pod_resource_quantity()`'s own real-
  scheduler algorithm: `max(sum of regular-container requests, largest
  single init-container request)` -- init containers run one at a time,
  never concurrently with each other or the regular containers, so only
  the largest one can ever matter, never a sum across them. Closes this
  section's own previously-named approximation ("sums only regular
  containers' requests, not init containers"); see §13 for the full
  design and live-verification. Still does not model ephemeral-storage
  or extended resources (e.g. GPUs) in v1 -- named as a real gap, not
  silently ignored.
- **Taints and tolerations**: every `Node.spec.taints` entry checked
  against the Pod's `spec.tolerations` for a real match (key, operator
  `Equal`/`Exists`, value, effect `NoSchedule`/`PreferNoSchedule`/
  `NoExecute` -- `PreferNoSchedule` is reported as a soft deterrent, not a
  hard block, matching real scheduler behavior).
- **Node selector**: `Pod.spec.nodeSelector`, an exact label-equality
  check against `Node.metadata.labels` -- the simplest, oldest placement
  constraint, and the one with zero ambiguity in how to evaluate it.
- **Required node affinity**: `Pod.spec.affinity.nodeAffinity.
  requiredDuringSchedulingIgnoredDuringExecution`, evaluated per the real
  `NodeSelectorRequirement` operator semantics (`In`/`NotIn`/`Exists`/
  `DoesNotExist`/`Gt`/`Lt`) against `Node.metadata.labels`.
- **Namespace ResourceQuota**: `core.list_namespaced_resource_quota()`'s
  own `status.hard`/`status.used` compared against the candidate Pod's own
  request -- this is simple, exact arithmetic (no scheduler internals to
  approximate), so it's evaluated for real rather than flagged
  inconclusive.
- **Namespace LimitRange**: `core.list_namespaced_limit_range()`'s own
  `type: Container` `min`/`max` bounds compared against each container's
  own explicit `requests`/`limits` (cpu/memory) -- closes this section's
  own previously-named "Admission constraints generally: Partial -- only
  ResourceQuota; no LimitRange" gap. See Section 12 for exactly what's
  modeled and what's deliberately not.
- **Required pod affinity / anti-affinity**: `Pod.spec.affinity.
  podAffinity`/`podAntiAffinity`'s own
  `requiredDuringSchedulingIgnoredDuringExecution` terms (ANDed, per real
  Kubernetes semantics -- unlike node affinity's OR'd `nodeSelectorTerms`),
  evaluated against the labels of every other already-scheduled Pod in the
  cluster via a second, dedicated snapshot
  (`node_inventory.py::build_pod_label_inventory()`), cross-referenced by
  `topologyKey` against each Node's own labels. A term using
  `namespaceSelector` needs a separate per-Namespace-labels call this
  engine doesn't make, so it is never guessed at: an affinity term that
  can't be proven met is treated as **not satisfied** (never claim a
  required condition holds without evidence), and an anti-affinity term
  that can't be proven *violated* is treated as **satisfied** (never
  fabricate a problem it hasn't earned the evidence for) -- the same
  unevaluatable term is also named in `unevaluated_constraints` either way,
  so a caller knows the required-side result may be incomplete.
- **Topology spread constraints**: every `topologySpreadConstraint`'s
  `maxSkew`/`topologyKey`/`labelSelector` evaluated for real -- per-domain
  Pod counts across every topology value any candidate Node has for that
  key (not just the one candidate node), with the candidate Pod
  hypothetically added to its own target domain before checking
  `max(counts) - min(counts) > maxSkew`. `whenUnsatisfiable=ScheduleAnyway`
  is a soft preference, reported but never blocking -- the same soft/hard
  split `PreferNoSchedule` taints already established. `minDomains`,
  `matchLabelKeys`, `nodeAffinityPolicy`, and `nodeTaintsPolicy` (newer,
  less commonly set fields) are not evaluated, a documented v1
  simplification, not an oversight.

**Detected and reported, explicitly not evaluated** (the finding names
that the constraint exists and why it isn't simulated, rather than silently
ignoring it or guessing):

- **Preferred node affinity** (the `preferred...` half of `nodeAffinity`)
  -- a soft scoring preference, not a hard constraint; reported as present,
  never treated as a reason a Pod can't be scheduled.
  the required half above checked for real.
- **Preferred pod affinity / anti-affinity** -- same treatment as
  preferred node affinity: a soft scoring preference, reported as present,
  never a reason a Pod can't be scheduled. (The *required* half is
  evaluated for real, above.)
- **Pod priority / preemption** -- `Pod.spec.priority`/`priorityClassName`
  are read and reported (no extra API call needed, already on the Pod
  object); whether a *lower*-priority Pod could actually be preempted to
  make room is not simulated (that needs the preemption's own complex
  victim-selection logic, well beyond "explain and simulate" scope).

This split is itself the deliverable, not an implementation detail: a
`SchedulingFinding`'s `evaluated_constraints` and
`unevaluated_constraints` lists make the boundary visible to every caller,
API and CLI alike -- never silently blending "didn't block it" with
"couldn't check it."

## 3. Module layout

```
src/kubesentinel/scheduling/
  __init__.py
  node_inventory.py   # list_node() -> list[NodeSnapshot]; sums existing Pod
                       # requests per node (the scheduler's own accounting unit)
  constraints.py       # pure functions: (pod_spec, node) -> ConstraintResult,
                        # one per evaluated constraint type above
  analyzer.py           # orchestrates: for a target Pod (or a hypothetical
                         # spec), runs every node through every constraint,
                         # produces a SchedulingFinding
  service.py            # SchedulingService: the only module with side effects
                         # (cluster reads + persistence), mirrors
                         # OptimizationService's own shape exactly
```

`models/scheduling.py`, `persistence/scheduling_repo.py` (a sibling behind
`persistence/interface.py`, same pattern as `optimization_repo.py`),
`api/scheduling_routes.py` (a new `APIRouter`), new CLI commands in
`cli/main.py` -- all following `MASTER_ARCHITECTURE.md`'s one established
pattern exactly, not a new shape.

## 4. Data model

```python
class ConstraintType(str, Enum):
    RESOURCE_FIT = "RESOURCE_FIT"
    TAINT_TOLERATION = "TAINT_TOLERATION"
    NODE_SELECTOR = "NODE_SELECTOR"
    REQUIRED_NODE_AFFINITY = "REQUIRED_NODE_AFFINITY"
    NAMESPACE_QUOTA = "NAMESPACE_QUOTA"
    REQUIRED_POD_AFFINITY = "REQUIRED_POD_AFFINITY"
    REQUIRED_POD_ANTI_AFFINITY = "REQUIRED_POD_ANTI_AFFINITY"
    TOPOLOGY_SPREAD = "TOPOLOGY_SPREAD"
    # Detected, not evaluated -- always end up in unevaluated_constraints
    # (the preferred half of node/pod affinity, or a required pod-affinity/
    # anti-affinity term using namespaceSelector, which the two REQUIRED_*
    # types above can't evaluate either):
    PREFERRED_NODE_AFFINITY = "PREFERRED_NODE_AFFINITY"
    POD_AFFINITY = "POD_AFFINITY"
    POD_ANTI_AFFINITY = "POD_ANTI_AFFINITY"

class ConstraintResult(BaseModel):
    constraint_type: ConstraintType
    satisfied: bool
    reasoning: str          # the specific fact, e.g. "node has 2000m
                             # allocatable cpu, 1800m already requested by
                             # other pods, this pod requests 500m -- short by 300m"

class NodeFitResult(BaseModel):
    node_name: str
    schedulable: bool       # AND of every evaluated constraint on this node
    constraint_results: list[ConstraintResult]

class SchedulingFinding(BaseModel):
    finding_id: str
    namespace: str
    pod_name: str | None     # None for a hypothetical (not-yet-created) spec
    workload_kind: str | None
    workload_name: str | None
    analyzed_at: datetime
    scheduler_message: str | None   # the real PodScheduled condition message, if any
    node_results: list[NodeFitResult]
    evaluated_constraints: list[ConstraintType]
    unevaluated_constraints: list[ConstraintType]
    any_node_schedulable: bool
    reasoning: str
    related_incident_ids: list[str] = []   # same by-ID link pattern as
                                            # ResourceRecommendation
```

No `proposed_*`/apply fields -- this capability never proposes a change to
apply, only explains and simulates. A future "suggest a lower request" or
"suggest a toleration" feature would be additive, not assumed here.

`related_incident_ids` is populated by `SchedulingService._open_incident_ids_for()`
-- the same dual-UID (the Pod's own UID, and its owning workload's UID,
deduped before querying) lookup against `IncidentRepository.find_open_by_resource_uid()`
that `OptimizationService` already uses for its own `ACTIVE_INCIDENT` flag,
reused by the exact same shape rather than reimplemented. Live-verified
against a real `pending-resources-demo` Pod whose owning Deployment had a
genuinely open incident: the finding correctly linked to it.

## 5. API and CLI

Matching the existing flat-path convention (no `/api/v1` prefix anywhere
in this repo):

| Route | Behavior |
|---|---|
| `POST /scheduling/analyze` | The only route that touches the cluster. Body: `{"namespace": str, "pod_name": str}` for a real Pod, or `{"namespace": str, "hypothetical_spec": {...}}` for a simulation. Persists and returns a `SchedulingFinding`. |
| `GET /scheduling/findings` | Reads persisted findings, filterable by `namespace`/`schedulable`. |
| `GET /scheduling/findings/{id}` | One finding in full. 404 if missing. |

CLI: `kubesentinel schedule-check <namespace> <pod-name>` (the common
case -- explain why an already-Pending Pod isn't scheduled) and
`kubesentinel scheduling-findings` (list persisted findings), mirroring
`optimize`/`recommendations`'s own naming shape.

## 6. Safety

Entirely read-only, the same guarantee Capability B already proves and the
same way: no `ClusterClients` method outside `list_*`/`get_*` anywhere in
`scheduling/`, checked with the same kind of HTTP-transport-level live test
`test_optimization_live_scenarios.py` already established. No apply path
exists or is implied.

## 7. RBAC

**No new `ClusterClients` field, and almost no new RBAC.** `nodes`
(get/list/watch) is already granted by the existing `rbac/clusterrole.yaml`
(added for Capability A's `NodeConditionRule`); `pods`/`deployments` are
already granted by `rbac/role.yaml`. The one genuinely new grant is
`resourcequotas` (get/list), for the namespace-quota check -- added as its
own `rbac/scheduling-role.yaml`, following the same "separate, independently-
auditable Role per capability" pattern `rbac/optimization-role.yaml`
already established, not folded into `role.yaml`.

## 8. Persistence

A new `scheduling_findings` table in the same SQLite file, via a new
`SQLiteSchedulingRepository` behind `persistence/interface.py` -- same
pattern as `SQLiteOptimizationRepository`. No raw per-node-per-constraint
data retained beyond what's in the finding's own JSON blob; no new
background job.

## 9. Observability

`kubesentinel_scheduling_analysis_total{result}` (schedulable/unschedulable/
failed), matching `kubesentinel_optimization_analysis_total`'s own
success/partial/failed shape.

## 10. Testing strategy

- **Unit**: each constraint function tested against hand-built `V1Node`/
  `V1Pod` objects (the same `kubernetes.client` model convention
  `tests/unit/factories.py` already uses) -- resource-fit math, every
  taint/toleration effect and operator combination, node-selector
  exact-match and mismatch, every `NodeSelectorRequirement` operator,
  quota exceeded/not-exceeded. Determinism (same input -> identical
  output), matching every other analyzer in this project.
- **Integration**: the read-only HTTP-transport check; API route
  validation/error codes.
- **Live**: against the real lab cluster -- the existing
  `pending-resources-demo` scenario (resource-fit failure, already
  live-verified by the troubleshooting engine, now also verified by this
  engine's own independent math reaching the same conclusion) plus a new,
  dedicated scenario for taint/node-selector mismatch (not currently
  demonstrated by any existing scenario).

## 11. Known limitation, stated once here rather than per-finding

The lab's *primary* kind cluster is single-node
(`IMPLEMENTATION_STATUS.md`/`PLATFORM_ROADMAP.md` already note this, and
it stays true -- `lab/kind-config.yaml` is unchanged). The analysis code
itself has always handled N nodes correctly (it's a loop over
`node_inventory`, not special-cased for one); what was missing was
anywhere to *show* that live. Closed as later, separate, optional
infrastructure: `lab/kind-config-multi-node.yaml` (1 control-plane + 2
workers) plus `lab/create-multi-node-cluster.sh`/
`lab/delete-multi-node-cluster.sh`, a second, separate cluster
(`kubesentinel-multi`) that never touches the primary one -- every
existing script, test, and live demonstration elsewhere in this project
still assumes the single-node `kind-kubesentinel` context, unaffected.

**Live-verified real cross-node trade-off**: created the multi-node
cluster, labeled exactly one of its two real workers
(`disktype=ssd`), and ran a real `POST /scheduling/simulate` call for a
hypothetical Pod with `nodeSelector: {disktype: ssd}` against it.
Confirmed `any_node_schedulable: true`, with the labeled worker
correctly `schedulable=true` and the *other* real worker correctly
`schedulable=false`, blocked specifically by `NODE_SELECTOR` (not
`RESOURCE_FIT` or anything else) -- the control-plane Node was
separately and correctly blocked by its own real
`node-role.kubernetes.io/control-plane:NoSchedule` taint. The exact
"fits node B, not node A, for a specific, named reason" result this
section used to say the lab couldn't show, now shown against three
genuinely different real Nodes, not a synthetic/mocked one.
Cluster deleted immediately afterward; `kubectl config use-context
kind-kubesentinel` restored the primary context.

## 12. LimitRange admission constraint

Closes this doc's own named gap in Section 2: "Admission constraints
generally: Partial -- only ResourceQuota; no LimitRange." Same
admission-time-only framing as `NAMESPACE_QUOTA` -- gated by the same
`check_quota` flag, meaningful only for a hypothetical spec
(`POST /scheduling/simulate`), never a real, already-existing Pod
(which already passed admission, or it wouldn't exist).

### 12.1 What's checked

For each `type: Container` item in every `V1LimitRange` in the
namespace, each container's own explicit `requests`/`limits` (cpu,
memory) is checked against that item's `min`/`max`. A request or limit
below `min`, or above `max`, is a violation -- the exact admission
rejection a real `LimitRanger` admission plugin would produce.

### 12.2 What's deliberately not modeled, named explicitly

- **`default`/`defaultRequest` injection.** A real admission controller
  silently fills in a container's missing request/limit from the
  LimitRange's own `default`/`defaultRequest` *before* checking it
  against `min`/`max`. This engine checks only what the container
  spec *explicitly* sets -- a container that specifies neither request
  nor limit at all is never flagged here, even if its namespace has a
  `min` it would otherwise need a default to satisfy. Re-deriving the
  full defaulting algorithm to close this precisely would be a
  meaningfully larger feature than "does an explicit value violate a
  bound"; named here as a real v1 boundary, not silently assumed
  covered.
- **`type: Pod`** (LimitRange's own aggregate-across-containers bounds)
  and **`type: PersistentVolumeClaim`** (storage request bounds) --
  only `type: Container` is evaluated, the same "documented v1 scope,
  not silently ignored" precedent `check_namespace_quota` already set
  for its own `requests.cpu`/`requests.memory`-only scope (no
  `limits.cpu`/`limits.memory`/object-count quotas either).
- **`maxLimitRequestRatio`** -- not evaluated.

### 12.3 RBAC

`LimitRange` is namespaced, same as `ResourceQuota` -- added to the
same existing `rbac/scheduling-role.yaml` Role rather than a new one.
`get`/`list` only.

### 12.4 Live-verified against the real cluster, both directions

Applied a real `LimitRange` (`min: {cpu: 200m, memory: 64Mi}`,
`max: {cpu: 2, memory: 1Gi}`) to the actual `kind-kubesentinel`
cluster's `kubesentinel-lab` namespace. A real `POST
/scheduling/simulate` call with a hypothetical container requesting
`cpu: 50m` (below the real `min`) correctly returned
`any_node_schedulable: false`, with `reasoning` naming the exact
violation ("`limit-range-demo`: container 'app' requests.cpu=50 below
min 200"). A second call with a container inside the real bounds
(`cpu: 300m` request, `500m` limit) correctly returned
`any_node_schedulable: true` with "within every LimitRange in this
namespace" -- both directions confirmed against real cluster state,
not asserted from one side only. Cleaned up afterward.

## 13. Init-container-aware resource fit

Closes Section 2's own previously-named approximation: "resource-fit
math sums only regular containers' requests, not init containers (the
real scheduler takes the *max* of init vs. regular container requests
per resource, not a sum)."

### 13.1 The algorithm, and why it's a max, not a sum

Init containers run strictly sequentially, one at a time, and never
concurrently with each other or with the Pod's regular containers --
they finish before any regular container starts. A Pod's real,
effective request for one resource is therefore the *greater* of (a)
the sum of every regular container's own request (these *do* run
concurrently with each other, so they really do add up), or (b) the
single largest init container's own request (never a sum across init
containers -- two 300m init containers never need 600m at once, only
the larger of the two, ever). `node_inventory.py::effective_pod_resource_quantity()`
implements exactly this, identically for `requests` and `limits`.

### 13.2 One function, three call sites, never three answers

The same real question -- "what is this Pod's effective resource
request/limit" -- is asked for three different Pods in three different
places: the candidate Pod being analyzed
(`analyzer.py::_pod_spec_request`), every *other* already-bound Pod on
a Node when accumulating `NodeSnapshot.requested`
(`node_inventory.py::_pod_requested`), and the same already-bound
Pods' `limits` for the overcommitment analysis
(`node_inventory.py::_pod_limits`, Milestone 36). All three now call
the one shared function rather than three separate, and previously
divergent, implementations -- before this closure, the candidate Pod's
own math (`analyzer.py`) and its neighbors' math (`node_inventory.py`)
were two separate copies of the *same* simplification, which could
only ever have drifted further apart, never converged, if fixed
separately later.

### 13.3 Native sidecars, closed as a later, separate follow-up

**Closed -- was named explicitly as a further v1 simplification layered
on top of §13.1's own closure.** `restartPolicy: Always` init containers
("native sidecars," Kubernetes 1.28+, stable since 1.29) actually run
concurrently with the Pod's regular containers for its whole lifetime --
the §13.1 max-not-sum algorithm alone treated every init container as
an ordinary, non-restarting one, letting a sidecar only compete in the
init-container max rather than adding its own request on top of
whatever runs alongside it.

**The real formula, per
[the Kubernetes docs' own worked example](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/#resource-sharing-within-containers),
not reinvented from scratch**: walk the init containers in their
declared order, maintaining a running total of every sidecar's own
request seen so far (a sidecar keeps running once started, so it's
still contributing by the time any later container starts). At the
exact moment any given init container is the one actively initializing,
what's genuinely in use is every earlier sidecar still running plus
that container's own request, whichever kind it is -- that's each
init-phase candidate. Once every init container has run, the final
steady state is every regular container plus every sidecar, all running
together. The effective value is the greatest of all of those moments.
`effective_pod_resource_quantity()` now implements exactly this,
verified against the Kubernetes docs' own numeric example (2+1+2 CPU
sidecar/plain-init/regular-container case, correctly resolving to 4, not
a naive 3 or 2) before ever touching the real cluster. Declaration order
matters for the init-phase candidates specifically (a sidecar only
counts toward a later init container's candidate, never an earlier
one's) but not for the final steady state, which only cares whether a
sidecar is restartable at all, not where it sits in the list -- both
properties are pinned by dedicated tests, not just the common case.

Reduces to exactly §13.1's original formula whenever no
`restartPolicy: Always` init container exists at all (the sidecar
running-total stays zero throughout), so every one of the three call
sites named in §13.2 is unaffected for a Pod that was already free of
sidecars, and continues to need no further special-casing.

### 13.4 Live-verified against the real cluster, isolating the exact delta

Applied a real Pod to the actual `kind-kubesentinel` cluster with a
10m-cpu regular container and a 700m-cpu init container. Measured the
real Node's "already requested" total via a `POST /scheduling/simulate`
call's own `RESOURCE_FIT` reasoning twice -- once with the Pod absent
(baseline: 950m) and once with it present (1650m) -- isolating this one
Pod's own contribution as exactly 700m: the init container's real
request, neither the regular container's 10m alone (what every call
site would have read before this fix) nor a naive 710m sum. Re-applied
and re-measured a second time to confirm reproducibility (1650m
again), then cleaned up.

### 13.5 Native sidecars, live-verified the same way, isolating the sidecar-specific delta

Applied a real Pod to the actual `kind-kubesentinel` cluster (server
version 1.36.1, well past the 1.29 stabilization) with a 10m-cpu
regular container, a 300m-cpu `restartPolicy: Always` init container
("sidecar"), and a 400m-cpu plain init container. Confirmed via the
real `kubelet`'s own reported container statuses, not assumed: the
sidecar's `state.running` never terminated, while the plain init
container's `state.terminated` (`reason: Completed`) started at the
exact same timestamp the sidecar did -- the real cluster genuinely ran
them concurrently, not just this engine's own model of it.

Measured the real Node's "already requested" total via the same
`POST /scheduling/simulate` technique as §13.4, before and after:
baseline 950m, with the Pod present 1650m -- a delta of exactly 700m
(300m sidecar + 400m plain init, both now counted in the final steady
state), not 400m (the pre-fix algorithm's own answer: `max(300, 400)`,
ignoring `restartPolicy` entirely) and not a naive 710m sum across
every container. Re-measured a second time to confirm reproducibility
(1650m again), then cleaned up.

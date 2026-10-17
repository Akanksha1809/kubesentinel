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
  `requests` from every *other* Pod currently bound to that Node (the
  scheduler's own accounting unit) plus the candidate Pod's own request.
  This reproduces the scheduler's `PodFitsResources` predicate exactly for
  cpu/memory; it does not model ephemeral-storage or extended resources
  (e.g. GPUs) in v1 -- named as a real gap, not silently ignored.
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

The lab's kind cluster is single-node
(`IMPLEMENTATION_STATUS.md`/`PLATFORM_ROADMAP.md` already note this). The
analysis code itself handles N nodes correctly (it's a loop over
`node_inventory`, not special-cased for one), but this project's own live
demo can only ever show "the one node passes or fails," never "fits node B
but not node A." Worth a second, optional multi-node `kind-config` variant
later if a true multi-node demo becomes valuable; not needed for the
analysis logic to be correct or tested.

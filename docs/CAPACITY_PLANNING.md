# Capacity planning and autoscaling advisor (Milestone 5 of the platform spec)

## 1. What already exists, reused here

Per the spec's own explicit instruction -- "extend the resource optimization
engine rather than creating a duplicate metrics collector" -- this milestone
is additive work inside `optimization/`, not a new top-level `capacity/`
package the way the scheduling and network engines were. Reused as-is, by
import, never re-derived:

- `optimization/metrics_providers.py::InMemorySampleStore`/`MetricsApiProvider`/
  `PrometheusProvider` -- the exact same usage-history collection this
  milestone reasons over. No second collector, no second sample store.
- `optimization/inventory.py::build_workload_inventory()` -- the same
  `WorkloadSnapshot` every other optimization check already uses. This
  milestone attaches one new field to it (`hpa`, see §4), nothing more.
- `optimization/service.py::OptimizationService.analyze()` -- the one
  existing entry point. Capacity findings are produced inside the same
  per-workload loop that already calls `analyze_workload()`, persisted
  through the same `ResourceRecommendation` row, surfaced through the same
  `/optimization/recommendations`, `kubesentinel recommendations`, and
  `kubesentinel recommendation` surfaces. No new API routes, no new CLI
  commands, no new persistence table -- `RecommendationCategory` is a closed
  enum and `category`/`data` are free-text/JSON columns already (confirmed
  by reading `persistence/optimization_repo.py`'s schema), so a new category
  value is the entire persistence change.
- `optimization/safety.py::apply_safety_checks()` -- already generic over
  every category. A capacity finding on an unhealthy workload, a system
  namespace, a Job/DaemonSet, or a workload with an open incident gets
  `UNHEALTHY_WORKLOAD`/`SYSTEM_NAMESPACE`/`*_CONSERVATIVE`/`ACTIVE_INCIDENT`
  and its proposal suppressed for free, with zero new code -- confirmed by
  reading the function: it iterates `recommendations` generically, never
  branching on `category`.
- `scheduling/node_inventory.py::build_node_inventory()` -- reused for the
  one genuinely new read this milestone needs: real Node allocatable-vs-
  already-requested totals, to answer "would additional replicas even have
  anywhere to go." This is cross-package reuse in the opposite direction
  from Milestone 4 (network reused scheduling's `requirement_matches`) --
  here `optimization/` imports from `scheduling/` -- which is fine: neither
  package imports the other today, so no cycle is introduced, and it avoids
  rebuilding Node allocatable math a third time (scheduling already built
  it once for placement, optimization would otherwise rebuild it for
  capacity).
- `diagnostics/rules/scheduling.py::classify_unschedulable_message` --
  already imported by `optimization/analyzer.py` for the existing
  `SCHEDULING` category; not re-touched, just noted as the precedent for
  "a workload already reports its own Pending reason via the scheduler's
  message," which this milestone's headroom check complements rather than
  duplicates (see §2).

## 2. Scope: what this milestone evaluates for real vs. defers

The spec names five specific reasoning cases ("when more replicas help vs.
when they don't"): memory leaks, single-threaded bottlenecks, missing
requests making autoscaling math meaningless, additional replicas that
would themselves be unschedulable, and an external dependency as the real
bottleneck. Each is addressed honestly below -- the spec's own instruction
not to oversell simple math as prediction applies to all of them.

**Evaluated for real:**

1. **HPA status** (`HorizontalPodAutoscalerV2`, read via the new
   `AutoscalingV2Api`): current/desired replicas, min/max bounds, and
   every configured metric's current value vs. its target, read live --
   not inferred. Matched to a `WorkloadSnapshot` by `scaleTargetRef`
   (`kind` + `name`; an HPA has no UID reference to its target, only a
   name, so the match is by name within the same namespace+kind, the only
   key available).
2. **Missing requests make autoscaling math meaningless**: when an HPA
   targets a `Resource` metric using `Utilization` (a percentage *of the
   request*, per the HPA's own documented semantics) and the target
   container has no request for that resource, this is flagged --
   deterministically, from the HPA spec and the container's own requests,
   not a guess.
3. **Additional replicas that would themselves be unschedulable**: when an
   HPA is currently at `maxReplicas` *and* its current metric value still
   exceeds target (i.e., it would scale further if it could), this
   analysis checks whether even one more Pod at this workload's own
   per-container request would fit anywhere in the real cluster -- a
   coarse, explicitly-approximate greedy first-fit against
   `scheduling/node_inventory.py`'s real Node allocatable-minus-requested
   totals (see §4), never a claim equivalent to the scheduling advisor's
   own per-constraint simulation. This deliberately does not re-run for
   every workload on every analysis pass (that would approach the
   scheduling advisor's own job) -- only for the specific, narrow
   "capped and still over target" condition named by the spec.
4. **Basic usage trend**: a simple two-half-mean comparison
   (`INCREASING`/`DECREASING`/`STABLE`/`INSUFFICIENT`) over the same pooled
   sample window `analyzer.py` already summarizes -- labeled explicitly in
   every finding's reasoning as "a trend observed over the sampled window,
   not a forecast," per the spec's own instruction against overselling
   simple math.
5. **Memory-leak-*shaped* pattern**: memory trend `INCREASING` over a
   long-enough window while the CPU trend is not also increasing --
   reported as a *pattern match*, explicitly never as a diagnosis ("this
   looks like unbounded growth independent of load; a leak is one
   explanation, not the only one"), and explicitly states the actionable
   consequence the spec asks for: more replicas would delay, not fix, a
   per-replica leak.
6. **Single-threaded/per-replica CPU ceiling pattern**: when at least
   `capacity_min_replicas_for_bottleneck_check` replicas each
   independently report CPU usage clustered in a narrow band around one
   full core, while the container's CPU limit allows meaningfully more --
   reported the same way, as a narrow pattern match on one specific
   signal, never a profiler-level claim. Computed from this call's own
   fresh per-Pod samples (not the cross-time pooled bag `analyzer.py` uses
   for CPU/MEMORY sizing), since the whole point is distinguishing
   *individual* replicas' behavior, which pooling across replicas
   deliberately discards for the sizing use case.

**Explicitly out of scope, stated once here rather than per-finding:**

- **An external dependency as the real bottleneck.** This project collects
  container CPU/memory usage only -- no request latency, no tracing, no
  dependency graph. There is no evidence source this engine has access to
  that could distinguish "this service is slow because of its own code"
  from "this service is slow because something it calls is slow." Every
  capacity finding says so explicitly when CPU/memory both look
  unremarkable (i.e., neither bottleneck pattern above fired) but an HPA
  is nonetheless not achieving its target -- named as a gap in the
  evidence, never silently assumed to be internal.
- **A true scheduling simulation for "would N replicas fit."** The
  headroom check in item 3 above is a cluster-wide, greedy, single-
  resource-at-a-time approximation -- not a re-run of the scheduling
  advisor's own per-constraint evaluation (taints, affinity, quotas). If a
  more precise answer is wanted, the existing `POST /scheduling/simulate`
  endpoint already does that job; this check only answers the narrower
  "is there obviously no room at all," and says so in its own reasoning
  text.
- **Any apply/write path.** No code anywhere in this project patches an
  HPA's `minReplicas`/`maxReplicas`, nor a workload's replica count.
  Exactly as true here as it already is for every other optimization
  category -- see RESOURCE_OPTIMIZATION_DESIGN.md §6.
- **StatefulSet-aware replica reasoning.** A StatefulSet's replicas are
  often not freely interchangeable (ordinal identity, per-replica storage)
  the way a Deployment's are. This milestone's capacity reasoning runs for
  any workload kind an HPA can target, but does not special-case
  StatefulSet semantics -- a StatefulSet's capacity finding, if any,
  reads identically to a Deployment's, which is a known simplification,
  not a claim that scaling is always safe for a StatefulSet.

## 3. Module layout

A new submodule inside the existing package, not a new package:

- `optimization/capacity.py` -- `build_hpa_inventory()` (one-shot HPA
  LIST via `AutoscalingV2Api`), `attach_hpa_snapshots()` (pure, matches
  HPAs to already-built `WorkloadSnapshot`s by scale-target
  kind+name), `compute_trend()` (pure, two-half-mean over a sample list),
  `detect_bottleneck_pattern()` (pure, the per-replica CPU-ceiling check
  over fresh per-pod samples), `compute_scale_headroom()` (reuses
  `scheduling/node_inventory.py::build_node_inventory`, pure greedy
  first-fit), `analyze_capacity()` (the one public entry point -- mirrors
  `analyzer.py::analyze_workload()`'s shape: pure function over
  already-fetched facts, returns `ResourceRecommendation | None`).
- `models/optimization.py` -- additive only (see §4); no new file, per
  the spec's own "extends" instruction applying to the data model too.
- `observer/client.py::ClusterClients` -- `autoscaling:
  AutoscalingV2Api` added (new, additive), the same pattern as every
  prior client addition.
- `optimization/service.py::OptimizationService.analyze()` -- calls
  `build_hpa_inventory()`/`attach_hpa_snapshots()` once per pass (same
  one-shot-per-analyze shape as the workload/metrics collection already
  there), then calls `analyze_capacity()` per workload alongside the
  existing `analyze_workload()` call, appending its result (if any) to
  the same `all_recommendations` list before persistence.

## 4. Data model (additive only)

```python
class TrendDirection(str, Enum):
    INCREASING = "INCREASING"
    DECREASING = "DECREASING"
    STABLE = "STABLE"
    INSUFFICIENT = "INSUFFICIENT"  # not enough samples/window to say

class HpaMetricStatus(BaseModel):
    metric_name: str          # e.g. "cpu", "memory", or a custom metric's name
    metric_type: str          # "Resource" | "Pods" | "Object" | "External" | "ContainerResource"
    target_utilization_percent: int | None = None
    target_average_value: str | None = None  # raw quantity string; this metric's "target" isn't always a %
    current_utilization_percent: int | None = None
    current_average_value: str | None = None

class HpaSnapshot(BaseModel):
    hpa_name: str
    namespace: str
    scale_target_kind: str
    scale_target_name: str
    min_replicas: int | None = None
    max_replicas: int
    current_replicas: int
    desired_replicas: int
    metrics: list[HpaMetricStatus] = Field(default_factory=list)
    # ScalingLimited condition's own status -- real evidence of "the HPA
    # itself is reporting it's capped," read verbatim, never inferred from
    # current_replicas == max_replicas alone (that could also mean "at max
    # and no longer under pressure").
    scaling_limited: bool = False
    scaling_limited_reason: str | None = None

# RecommendationCategory gains one new value:
class RecommendationCategory(str, Enum):
    CPU = "CPU"
    MEMORY = "MEMORY"
    SCHEDULING = "SCHEDULING"
    DATA_QUALITY = "DATA_QUALITY"
    CAPACITY = "CAPACITY"  # new

# WorkloadSnapshot gains one new field:
class WorkloadSnapshot(BaseModel):
    ...
    hpa: HpaSnapshot | None = None  # None means "no HPA targets this workload"
```

No change to `ResourceRecommendation` itself: a capacity finding never
sets `proposed_requests`/`proposed_limits` (there is no numeric resource
change being proposed, same as the existing `SCHEDULING` category) --
everything capacity-specific is carried in `reasoning`/`potential_benefits`/
`risks`/`safety_flags`, the same shape `SCHEDULING`/`DATA_QUALITY` already
use. New `safety_flags` values this milestone introduces: `HPA_AT_MAX`,
`HPA_AT_MIN`, `MISSING_REQUESTS_FOR_AUTOSCALING_MATH`,
`SCALING_WOULD_BE_UNSCHEDULABLE`, `POSSIBLE_MEMORY_LEAK_PATTERN`,
`POSSIBLE_SINGLE_THREADED_BOTTLENECK`.

New `Settings` fields (`config.py`), same conservative-defaults discipline
as every existing `optimization_*` threshold:

```python
capacity_trend_min_samples: int = 4
capacity_trend_change_ratio: float = 0.2       # +/-20% between window halves to call it a trend
capacity_leak_min_window_seconds: float = 900.0  # longer than the general history minimum --
                                                   # a leak pattern needs more than one short window
capacity_single_core_ceiling_millicores: int = 1000
capacity_single_core_band_millicores: int = 150
capacity_min_replicas_for_bottleneck_check: int = 2
capacity_min_limit_ratio_for_bottleneck_check: float = 1.5  # limit must allow meaningfully more than 1 core
```

## 5. API and CLI

No new routes, no new commands. `GET /optimization/recommendations?category=CAPACITY`
and `kubesentinel recommendations --category CAPACITY` work immediately
once the enum gains its new value -- confirmed by reading
`api/optimization_routes.py` (`category: RecommendationCategory | None`,
a typed Enum query param already) and `OptimizationRepository`'s
free-text `category` column. `GET /optimization/workloads` already
returns full `WorkloadSnapshot` dicts, so `hpa` appears there automatically.
The CLI's `--category` help string is updated to mention `CAPACITY`
(the only CLI change this milestone makes).

## 6. Safety

Entirely read-only, same as every prior optimization-engine capability:
`AutoscalingV2Api` is only ever used for `list_namespaced_horizontal_pod_autoscaler`/
`list_horizontal_pod_autoscaler_for_all_namespaces` (read-only verbs only
in RBAC, see §7). No code path patches an HPA or a workload's replica
count. The one new cross-package read (`scheduling/node_inventory.py`) is
itself already read-only.

## 7. RBAC

One new Role, following the same "smallest grant this specific code
calls" discipline as every prior Role:

```yaml
# rbac/capacity-role.yaml -- a sixth, separate Role, for the capacity
# planning & autoscaling advisor (Milestone 5 of the platform spec)
rules:
  - apiGroups: ["autoscaling"]
    resources: ["horizontalpodautoscalers"]
    verbs: ["get", "list"]
```

Nodes (for `scheduling/node_inventory.py::build_node_inventory`) are
already granted by `clusterrole.yaml`; Pods/Deployments/StatefulSets/
DaemonSets/Jobs by `role.yaml`/`optimization-role.yaml`. This capability's
own package needs exactly one new grant.

## 8. Persistence

None. See §1/§4 -- `WorkloadSnapshot`/`ResourceRecommendation` are
JSON-blob-serialized as a whole; a new optional field and a new enum
value need no schema migration, confirmed by reading
`persistence/optimization_repo.py`.

## 9. Observability

No new Prometheus metric. `kubesentinel_optimization_recommendations_total{category="CAPACITY"}`
already exists the moment the enum gains its value (the label is
populated dynamically from `ResourceRecommendation.category.value`, not a
pre-declared closed set per metric) -- confirmed by reading
`metrics/registry.py` and `service.py::analyze()`'s own
`by_category`/`.labels(category=...)` loop, which is already generic.

## 10. Testing strategy

- **Unit**: `compute_trend()` against hand-built sample sequences
  (clearly increasing, clearly decreasing, flat, too-few-samples);
  `attach_hpa_snapshots()` matching/non-matching scale-target
  kind+name; `detect_bottleneck_pattern()` with N replicas clustered at
  the ceiling vs. spread out vs. too few replicas; `compute_scale_headroom()`
  against hand-built Node snapshots (fits on one node, fits nowhere, fits
  exactly at the boundary); every `analyze_capacity()` branch
  (no HPA at all -> no finding when nothing else fires; HPA healthy, not
  capped -> status-only finding; HPA at max + still over target + no
  headroom -> `SCALING_WOULD_BE_UNSCHEDULABLE`; missing request for a
  Utilization metric -> `MISSING_REQUESTS_FOR_AUTOSCALING_MATH`; leak
  pattern; bottleneck pattern). Determinism, matching every other analyzer.
- **Integration**: the existing read-only HTTP-transport check
  (`test_optimization_live_scenarios.py`'s sibling) extended to also cover
  the new `AutoscalingV2Api` calls.
- **Live**: a new lab scenario pairing a Deployment with a real HPA,
  chosen specifically to exercise the "capped and still over target"
  condition deterministically (e.g. a CPU-bound container with a
  deliberately low `maxReplicas` under a synthetic load, or -- more
  reliably reproducible on a single-node kind cluster without needing
  sustained synthetic load -- an HPA whose `minReplicas`/`maxReplicas`
  are both set low against a workload whose per-replica request is large
  enough that even `maxReplicas` worth of headroom doesn't exist on this
  cluster's one Node, making `SCALING_WOULD_BE_UNSCHEDULABLE` reproducible
  without load generation at all). The exact mechanism is decided during
  implementation and confirmed live, not assumed from this doc.

## 11. Known limitations, stated once here rather than per-finding

- No external-dependency/latency signal exists in this project at all
  (see §2) -- every capacity finding where neither the leak nor
  bottleneck pattern fires, but an HPA is still not meeting target, says
  so explicitly.
- The single-threaded-bottleneck heuristic is a narrow pattern match on
  one specific signal (a CPU plateau near one core across several
  replicas) and will not detect other single-threaded shapes (e.g. a
  bottleneck below one core, or a bottleneck that only appears under a
  specific request type this engine has no visibility into).
- The scale-headroom check is a cluster-wide, single-resource-at-a-time,
  greedy first-fit approximation -- not the scheduling advisor's own
  per-constraint simulation (taints, affinity, quotas are not considered
  here at all). A workload that this check says "would fit" could still
  be blocked by a constraint the scheduling advisor would catch; this
  check only ever strengthens a negative ("no room anywhere, by the
  simplest possible measure"), never a positive guarantee.
- StatefulSet replica semantics are not modeled specially (see §2).

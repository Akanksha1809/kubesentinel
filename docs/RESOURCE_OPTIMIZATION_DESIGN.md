# Resource Optimization Engine: design

Milestone 1 deliverable: repository audit, baseline, and integration plan for
extending KubeSentinel with a read-only Kubernetes resource right-sizing
engine, before any optimization code is written.

## 1. Repository audit (as of this writing)

**What exists and is reused as-is:**

| Area | File(s) | Reused how |
|---|---|---|
| Kubernetes client bootstrap | `observer/client.py::ClusterClients`/`build_clients()` | Extended (see §3), never replaced -- same kubeconfig/in-cluster loading path. |
| Pydantic model conventions | `models/*.py` | New `models/optimization.py` follows the same style (typed enums, `Field(default_factory=...)`, no raw dicts). |
| SQLite persistence pattern | `persistence/interface.py` + `sqlite_repo.py` | New `RecommendationRepository` ABC + `SQLiteRecommendationRepository`, same `db_path`, new table, same connection-per-repo-instance + `threading.Lock` pattern. No new database. |
| FastAPI app | `api/app.py` (flat, no sub-routers yet) | New `api/optimization_routes.py` using `fastapi.APIRouter`, included via `app.include_router()` -- first use of a sub-router in this codebase, but a standard FastAPI pattern, not a new dependency. Existing routes untouched. |
| CLI | `cli/main.py` (Typer) | New `optimize`/`recommendations` commands added the same way `explain`/`dry-run` were. |
| Prometheus metrics | `metrics/registry.py` | New bounded-cardinality metrics appended to the same module/registry. |
| Policy/RBAC conventions | `policy/engine.py`, `rbac/*.yaml` | New, separate, narrowly-scoped Role/ClusterRole files following the existing "one capability, one Role" pattern (`role.yaml` observer, `remediation-role.yaml` writes). Optimization adds its own read-only Role rather than widening an existing one. |
| Owner-reference extraction | `observer/inspector.py::resource_ref_from_pod` | Only resolves **one** level (Pod -> immediate owner, e.g. ReplicaSet) -- sufficient for incident identity, but **not** sufficient for optimization, which needs the full chain up to the Deployment/StatefulSet/DaemonSet. A new `optimization/ownership.py` walks the chain; `inspector.py` is not modified (its one-level behavior is correct for what it's used for today). |
| AI boundary pattern | `ai/client.py` (`AiClient` Protocol) | Mirrored for `optimization/metrics_providers.py` (`MetricsProvider` Protocol) -- same "swap the implementation without touching the caller" shape. |

**Current implementation status** (confirmed by reading the code, not assumed):
- Troubleshooting/self-healing pipeline: fully implemented through Milestone 9 of the original build (observe -> diagnose -> policy -> remediate -> verify), plus Milestone 11's optional local-AI summarization. See TODO.md.
- No resource-optimization code exists yet anywhere in the repository.
- No Kubernetes **quantity parsing** utility exists anywhere (`grep`-confirmed) -- CPU/memory string parsing (`"100m"`, `"0.5"`, `"128Mi"`, `"1Gi"`, `"2"`) is new, foundational work for Milestone 2.
- The `kubernetes` Python client (pinned `30.1.0`) has no dedicated typed Metrics API client. The Metrics API (`metrics.k8s.io/v1beta1`) is an aggregated API with no generated model in this client version; it must be queried via `CustomObjectsApi.list_namespaced_custom_object`/`list_cluster_custom_object`, which returns plain dicts. `ClusterClients` needs a `custom_objects: CustomObjectsApi` field added (additive; nothing existing breaks).
- `AppsV1Api` (already a client dependency) covers Deployments, ReplicaSets, StatefulSets, DaemonSets. Jobs require `BatchV1Api`, not currently constructed anywhere -- added to `ClusterClients` alongside `custom_objects`.

**Baseline test run** (this session, before any optimization code):
```
./scripts/test.sh
  ruff check src tests: All checks passed!
  mypy src/kubesentinel: Success: no issues found in 54 source files
  pytest tests/unit: 234 passed in 2.33s
```
`./scripts/test.sh --all` (live cluster) was not re-run for this baseline: the dev kind cluster is currently sharing its host VM with an unrelated project and showing real resource contention (confirmed via `docker stats` -- a second kind cluster using 243% CPU / 50% of the VM's memory), which produces environment flakiness unrelated to KubeSentinel (see TODO.md's existing note on this). Live verification for each optimization milestone below will still be done against the real cluster as it becomes responsive; this is noted so a slow/flaky live check during this extension isn't mistaken for a regression.

**Cluster environment, confirmed live:**
- `metrics-server` is **not installed** on the lab kind cluster (`kubectl -n kube-system get deployment metrics-server` -> `NotFound`). The optimization engine must handle this as a first-class, honest "no metrics available" state, not a crash -- this is the normal state for a fresh local kind cluster, not a bug. Local install instructions are added to the docs (§9).
- No Prometheus instance is running. Prometheus support is therefore an **optional**, feature-flagged provider from day one, never a hard dependency.

**Integration risks identified and how they're addressed:**
1. *Risk*: treating every Pod as its own workload would massively over-count replicas as separate "workloads" and produce one recommendation per Pod instead of one per Deployment/StatefulSet. *Mitigation*: `optimization/ownership.py` resolves the full owner chain (Pod -> ReplicaSet -> Deployment, or Pod -> StatefulSet/DaemonSet/Job directly) before any analysis runs; the analyzer only ever sees workload-level aggregates, never a bare Pod list (except for genuinely standalone Pods, which the spec explicitly allows analyzing directly).
2. *Risk*: a full cluster scan on every API GET would be slow and hammer the API server. *Mitigation*: see §4 (analysis is explicitly triggered, cached, and timestamped; GETs serve the cache).
3. *Risk*: silently fabricating usage numbers when metrics are missing. *Mitigation*: every recommendation carries an explicit `evidence_quality` and `data_coverage`; "no data" is a distinct, visible state from "zero usage," enforced in the model layer (§5) so a caller cannot accidentally treat them the same way.
4. *Risk*: duplicating the existing policy/approval machinery with a parallel one for optimization. *Mitigation*: the optimization engine's `review` endpoint only ever transitions a recommendation among `new/reviewed/approved/rejected/expired` -- it never calls `RemediationService`, `PolicyEngine`, or `RemediationExecutor`. No "apply" endpoint exists in this phase (see §6); when apply work is scoped later, it is expected to produce a new `ActionType` (e.g. `PATCH_RESOURCES`) that flows through the *existing* `PolicyEngine`/`RemediationExecutor`, not a new parallel executor.

## 2. Product boundary (restated precisely)

Two engines, one shared Kubernetes client/config/persistence/metrics/CLI/API
surface, explicit interfaces, no tight coupling:

```
Troubleshooting engine (existing)          Optimization engine (new)
  observer -> diagnostics -> incidents        optimization.inventory -> optimization.analyzer
  -> policy -> remediation -> verification    -> optimization.safety -> optimization.service
         |                                              |
         +------------------ shared ---------------------+
         ClusterClients (Kubernetes API access)
         Settings (config)
         SQLite file (separate tables)
         Prometheus metric registry (separate metric names)
         FastAPI app (separate routes, same app)
         CLI (same Typer app, new commands)
```

Neither engine imports the other's internals. The only cross-references are:
- `optimization/service.py` takes an `IncidentRepository` (the existing
  interface) as a constructor dependency, to check for active incidents on a
  workload and to look up related incident IDs -- it calls only
  `IncidentRepository.list()`/`find_open_by_resource_uid()`, already-public
  methods, nothing new added to that interface.
- Nothing in `diagnostics/`, `remediation/`, or `policy/` imports anything
  from `optimization/`. The dependency arrow points one way.

## 3. Module layout (adapted to this repository)

```
src/kubesentinel/
  observer/
    client.py          EXTENDED: + custom_objects (CustomObjectsApi), + batch (BatchV1Api)
    inspector.py        unchanged
  optimization/          NEW
    __init__.py
    quantities.py        CPU/memory string <-> normalized-int parsing (millicores, bytes)
    metrics_providers.py MetricsProvider Protocol; MetricsApiProvider (metrics-server,
                          via CustomObjectsApi); PrometheusProvider (optional, HTTP range
                          queries, stdlib urllib like ai/client.py); InMemorySampleStore
                          (bounded per-container ring buffer, the "poor man's history"
                          when only metrics-server is available)
    ownership.py          Resolves Pod -> ReplicaSet -> Deployment / Pod -> StatefulSet /
                          Pod -> DaemonSet / Pod -> Job / standalone Pod. Pure functions
                          over already-fetched objects, like inspector.py.
    inventory.py          Builds WorkloadSnapshot objects: workload identity, containers,
                          requests/limits, replica/health summary, owning-chain-resolved
                          Pods, Node allocatable context, relevant Events.
    analyzer.py           Deterministic CPU/memory analysis: underutilization, right-
                          sizing candidates, pressure/waste flags. No ML, no AI call.
    safety.py             Validates a recommendation before it's ever surfaced: active
                          incident on the workload, workload health, namespace policy,
                          data sufficiency, staleness.
    service.py            OptimizationService: orchestrates inventory -> analyzer ->
                          safety -> persistence -> metrics. The only entry point the
                          API/CLI call into this package through.
  models/
    optimization.py      NEW: ResourceRecommendation, WorkloadSnapshot, UsageSummary,
                          EvidenceQuality enum, RecommendationStatus enum, RecommendationCategory enum
  persistence/
    interface.py          EXTENDED: + RecommendationRepository ABC (new, alongside
                          IncidentRepository -- not merged into it; different aggregate)
    sqlite_repo.py        unchanged
    optimization_repo.py  NEW: SQLiteRecommendationRepository (same db_path, new table)
  api/
    app.py                EXTENDED: app.include_router(optimization_router)
    optimization_routes.py NEW
  cli/
    main.py                EXTENDED: optimize / recommendations commands
  metrics/
    registry.py            EXTENDED: kubesentinel_optimization_* metrics appended
  config.py                EXTENDED: optimization_* settings appended
rbac/
  optimization-role.yaml         NEW: namespaced read-only (statefulsets, daemonsets,
                                  jobs, pods.metrics.k8s.io) in kubesentinel-lab
  optimization-rolebinding.yaml  NEW
  optimization-clusterrole.yaml  NEW: nodes.metrics.k8s.io (cluster-scoped, like the
                                  existing node-observer exception)
  optimization-clusterrolebinding.yaml NEW
```

No new top-level `storage/` or `policy/`-vs-`remediation/` restructuring: the
repository already names its persistence layer `persistence/` and its action
gate `policy/` + `remediation/`, so the proposal's `storage/` is implemented
as `persistence/` and `policy/` is reused, not duplicated.

## 4. Data flow

```
                 +-----------------------------------------------------------+
                 |  POST /optimization/analyze  (or `kubesentinel optimize`)  |
                 +------------------------------+------------------------------+
                                                v
   AppsV1Api/BatchV1Api/CoreV1Api  ---->  inventory.py  ---->  WorkloadSnapshot[]
   (Deployments/StatefulSets/                 |  (ownership.py resolves the
    DaemonSets/Jobs/Pods/Nodes/Events)         |   full owner chain per Pod)
                                               v
   CustomObjectsApi (metrics.k8s.io)  --->  metrics_providers.py  --->  UsageSummary
   + optional Prometheus HTTP            (current snapshot, optionally
                                           bounded in-memory samples,
                                           optionally real Prometheus range)
                                               |
                                               v
                                         analyzer.py  --->  ResourceRecommendation[]
                                               |             (not yet persisted)
                                               v
                                         safety.py  --->  safety flags + status
                                        (checks IncidentRepository for an
                                         active incident on this workload)
                                               |
                                               v
                           SQLiteRecommendationRepository.save_batch(...)
                                               |
                                               v
         kubesentinel_optimization_* Prometheus metrics updated (count, outcome, freshness)
                                               |
                                               v
               GET /optimization/recommendations[...]  <-- serves the persisted,
               GET /optimization/summary                   cached result; never
               GET /optimization/workloads                 re-scans the cluster
                                                             on a GET
```

**Why `/analyze` is explicit rather than always-on:** the spec's own
constraint ("do not make a full cluster scan on every API request") and
"avoid unbounded background tasks" point the same direction -- a
caller-triggered, timestamped analysis pass that *persists* its result and a
`generated_at`/`data_as_of` freshness marker is simpler and more honest than
either (a) scanning on every GET or (b) a silent background poller whose
cadence and resource cost aren't visible to the operator. A future milestone
could add an optional periodic scheduler (reusing the existing
`ObserverService` thread pattern) behind a setting; this design doesn't
preclude it but doesn't build it yet, since the spec didn't ask for it and it
adds a real "always-running thread" cost this phase doesn't need.

## 5. Data model

`models/optimization.py` (sketch -- exact fields finalized in Milestone 2):

```python
class RecommendationCategory(str, Enum):
    CPU = "CPU"
    MEMORY = "MEMORY"
    SCHEDULING = "SCHEDULING"
    DATA_QUALITY = "DATA_QUALITY"   # e.g. missing requests/limits entirely

class EvidenceQuality(str, Enum):
    # Distinct from Confidence (diagnostics/models/diagnosis.py) deliberately --
    # this is "how much data backs this," not "how sure is the hypothesis."
    INSUFFICIENT = "insufficient"    # no usable samples; never emits a resize number
    SNAPSHOT_ONLY = "snapshot_only"  # one or a few metrics-server current samples
    LIMITED_HISTORY = "limited_history"  # bounded in-memory sample window, no Prometheus
    HISTORICAL = "historical"        # Prometheus range query covering the full window

class RecommendationStatus(str, Enum):
    NEW = "NEW"
    REVIEWED = "REVIEWED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    # No "APPLIED"/"FAILED" in this phase -- there is no apply path yet (see §6).

class ResourceQuantity(BaseModel):
    cpu_millicores: int | None
    memory_bytes: int | None

class UsageSummary(BaseModel):
    evidence_quality: EvidenceQuality
    sample_count: int
    window_seconds: float | None   # None when evidence_quality is INSUFFICIENT/SNAPSHOT_ONLY
    observed_at: datetime          # when this summary was computed, not when generated
    cpu_typical_millicores: int | None
    cpu_peak_millicores: int | None
    memory_typical_bytes: int | None
    memory_peak_bytes: int | None   # or configured high percentile, see §7
    throttled: bool | None          # None = unknown (no cpu.stat data), not "no"

class ResourceRecommendation(BaseModel):
    recommendation_id: str
    namespace: str
    workload_kind: str              # Deployment | StatefulSet | DaemonSet | Pod
    workload_name: str
    container_name: str
    category: RecommendationCategory
    current: ResourceQuantity        # requests
    current_limits: ResourceQuantity
    usage: UsageSummary
    proposed: ResourceQuantity | None  # None when evidence doesn't support a number
    reasoning: str                   # human-auditable, no LLM involved
    potential_benefits: list[str]
    risks: list[str]
    related_incident_ids: list[str]
    safety_flags: list[str]          # e.g. "ACTIVE_INCIDENT", "UNHEALTHY_WORKLOAD",
                                      # "SYSTEM_NAMESPACE", "JOB_EXCLUDED"
    status: RecommendationStatus
    created_at: datetime
    expires_at: datetime             # staleness horizon; re-GETting after this
                                      # triggers "stale, re-run /analyze" rather
                                      # than silently serving an old number
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_notes: str | None
```

Units are explicit and normalized at the boundary: `quantities.py` is the
only place that parses/formats Kubernetes quantity strings; every internal
field is a plain `int` (millicores, bytes), never a string, so no downstream
code can misinterpret `"1"` vs `"1m"` vs `"1Gi"`.

## 6. Safety and policy (read-only, enforced structurally)

- `optimization/service.py` has no import of, or reference to, any
  Kubernetes **write** client method. `inventory.py`/`analyzer.py` only ever
  call `list_*`/`get_*` methods. This is checked, not just asserted: the
  integration test suite (Milestone 7) asserts the mocked write-capable
  methods on the Kubernetes clients (`patch_*`, `replace_*`, `delete_*`) are
  never called during a full analyze pass.
- No `apply` endpoint exists. `POST /optimization/recommendations/{id}/review`
  only accepts `{"decision": "approved"|"rejected"|"reviewed", "notes": "..."}`
  and transitions status accordingly -- it never touches the cluster. This
  matches the spec's explicit instruction not to expose a misleadingly-named
  endpoint for unimplemented behavior.
- A future "apply" milestone (not part of this one) would need: a new
  `ActionType.PATCH_RESOURCES` (or similar) in `models/action.py`, a new
  `RemediationExecutor` method that patches
  `spec.template.spec.containers[].resources` via a **typed** patch body (not
  `.to_dict()` -- see TODO.md #11 for why that's a real, previously-caught
  footgun), and the *existing* `PolicyEngine`/`RemediationService` lifecycle
  (`propose -> approve -> execute -> verify`) -- not a parallel approval
  system. This design deliberately does not build that executor yet.
- `safety.py` flags, never silently excludes, workloads it won't recommend
  changes for: an active non-resolved incident on the same resource UID (via
  `IncidentRepository.find_open_by_resource_uid`, walked up through the owner
  chain), a workload in a system namespace (`kube-system`, `kube-public`,
  `kube-node-lease` -- excluded by default, configurable), Jobs (conservative
  by default per the spec), and a workload whose Pods aren't currently
  healthy (non-Ready, recent OOMKilled, high restart count) -- these still
  get a recommendation *row* with `safety_flags` populated and a
  `DATA_QUALITY`-leaning `evidence_quality`, not silence, so an operator can
  see "KubeSentinel looked at this and chose not to recommend a number,"
  distinct from "KubeSentinel never looked at this."
- RBAC: strictly additive, read-only (§3's new Role/ClusterRole), no
  `cluster-admin`, no write verbs, no Secrets. See RBAC_AND_THREAT_MODEL.md's
  eventual update.

## 7. Algorithms (deterministic, no LLM)

Full parameter defaults are finalized in Milestone 4 with unit tests pinning
them; the shape is fixed now so later milestones don't need to redesign the
model:

- **Underutilization / right-sizing** requires `EvidenceQuality` to be at
  least `LIMITED_HISTORY` with a configurable minimum sample count and window
  coverage (e.g. default: at least N samples spanning at least M minutes)
  before proposing a *number* -- below that threshold the recommendation is
  still emitted (so the workload is visibly "considered") but `proposed` is
  `None` and `reasoning` says why, matching "do not recommend aggressive
  downsizing from a single sample."
- **CPU**: proposed request derived from a configurable percentile of
  observed usage (not the bare average) plus a configurable safety margin
  (multiplicative, e.g. +20% by default), never below a configurable floor.
  CPU *limits* are treated more conservatively than requests -- throttling
  data (`container_cpu_cfs_throttled_seconds_total`-style, only available via
  Prometheus) raises `risks`, never silently relaxes the recommendation.
  **Implemented** (previously scaffolded-but-unpopulated, closed as part of
  Milestone 1's own "stabilize the core" audit, item 1):
  `analyzer.py::_check_throttled()` runs the real
  `increase(container_cpu_cfs_throttled_seconds_total{...}[5m])` query
  whenever Prometheus is configured; a confirmed-throttled result both
  states the fact explicitly in `reasoning` (distinct from the merely
  hypothetical "this *could* throttle" phrasing used when no Prometheus
  signal is available) and vetoes a proposed CPU decrease, the same "real
  evidence of pressure vetoes a decrease" rule already applied to a
  recorded OOM kill. Unit-tested against a mocked Prometheus response
  (the same verification tier every Prometheus-dependent code path in
  this project uses, since Prometheus itself has never been deployed in
  this project's own lab -- it remains an optional, disabled-by-default
  upgrade; see TODO.md).
- **Memory**: proposed request/limit derived from observed **peak** (or a
  configurable high percentile, e.g. p99) over the window, never the
  average -- the spec is explicit that memory is not CPU, and a transient
  average is actively dangerous for a limit. Any OOMKilled signal for this
  workload (checked via the existing `inspector.py` evidence shape, reused,
  not reimplemented) sets a `safety_flags` entry and *increases* required
  evidence before proposing a decrease, never auto-increases a limit as a
  side effect.
- **Pressure/waste** is a separate, cheaper pass that doesn't need historical
  data: missing requests/limits entirely (`DATA_QUALITY`), usage near a
  configured percentage of the limit (`MEMORY`/`CPU`, pressure not waste),
  and `Pending` Pods whose `PodScheduled=False/Unschedulable` message
  mentions insufficient resources (reusing
  `diagnostics/rules/scheduling.py`'s existing message classification
  logic by reference/import, not a copy) linked as `SCHEDULING` category
  evidence of under-provisioned cluster capacity, not workload waste.

## 8. API (adapted prefix convention)

The existing API has **no** `/api/v1` prefix anywhere (`/incidents`,
`/metrics`, ...). Per the spec's own instruction to adapt prefixes to the
current API rather than import the proposal's literal paths, optimization
routes follow the same flat convention:

| Spec's proposed path | Actual path in this repo |
|---|---|
| `GET /api/v1/optimization/summary` | `GET /optimization/summary` |
| `GET /api/v1/optimization/workloads` | `GET /optimization/workloads` |
| `POST /api/v1/optimization/analyze` | `POST /optimization/analyze` |
| `GET /api/v1/optimization/recommendations` | `GET /optimization/recommendations` |
| `GET /api/v1/optimization/recommendations/{id}` | `GET /optimization/recommendations/{id}` |
| `POST /api/v1/optimization/recommendations/{id}/review` | `POST /optimization/recommendations/{id}/review` |

Filters: `namespace`, `workload`, `category`, `status` as query params on the
list endpoints, mirroring `GET /incidents?status=...&namespace=...`'s
existing convention exactly.

Error mapping (reusing `HTTPException`, the existing convention):

| Condition | Status |
|---|---|
| Workload/recommendation not found | 404 |
| Invalid filter/body (bad enum value, etc.) | 422 (FastAPI/Pydantic default) |
| Metrics provider unreachable or permission-denied | 200 with `evidence_quality=insufficient` + a surfaced `warnings` list on `/analyze`'s response -- a degraded analysis is not the same failure class as a broken request, and the spec requires honest partial results, not a hard error, when metrics are merely missing |
| `/analyze` itself fails unexpectedly (e.g. cluster unreachable entirely) | 503, matching `_require_remediation_service()`'s existing 503 convention for "no cluster connection" |
| Stale recommendation fetched past `expires_at` | 200, with `status` visibly `EXPIRED` in the body -- not an error, an honest label |

`/optimization/recommendations/{id}/review` reuses the project's one existing
auth gate pattern (`require_api_token`) for consistency, even though review
is not itself a cluster mutation -- it is a decision record, and the spec
asks for "require explicit approval," which this project already treats as
equivalent to "needs the same bar as other write-ish operations." `GET`
endpoints stay unauthenticated, matching every existing read endpoint.

## 9. Metrics setup (local, no cloud)

- **Metrics Server**: required for any usage data at all. Local install for
  kind:
  ```bash
  kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
  # kind's CA setup needs --kubelet-insecure-tls for a local cluster (not for prod):
  kubectl -n kube-system patch deployment metrics-server --type=json \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
  ```
- **Prometheus** (optional): any local Prometheus reachable over HTTP works;
  `optimization/metrics_providers.py::PrometheusProvider` is configured with
  a base URL (`KUBESENTINEL_OPTIMIZATION_PROMETHEUS_URL`, empty = disabled)
  and uses the same stdlib-`urllib`-only approach as `ai/client.py::OllamaClient`
  -- no new HTTP dependency.
- Without either, the engine still runs: `evidence_quality` never rises above
  `INSUFFICIENT`/`SNAPSHOT_ONLY`, and every recommendation says so explicitly.

## 10. Observability

New bounded-cardinality metrics (`metrics/registry.py`), matching the
project's existing label discipline (never a pod name, recommendation ID, or
incident ID as a label):

```python
optimization_analysis_total = Counter(..., ["status"])  # success | partial | failed
optimization_recommendations_total = Counter(..., ["category"])
optimization_last_success_timestamp = Gauge(...)  # unix seconds, no labels
optimization_metrics_data_available = Gauge(..., ["provider"])  # 1/0, provider in {metrics_api, prometheus}
```

## 11. Persistence and performance

- Same SQLite file (`db_path`), a new `recommendations` table (one row per
  recommendation, JSON blob + indexed columns, exactly mirroring
  `sqlite_repo.py`'s existing `incidents` table shape) plus a small
  `analysis_runs` table (one row per `/analyze` call: timestamp, workload
  count, provider availability, duration) for the freshness/status surface.
- No raw per-sample metrics are persisted. `InMemorySampleStore` is a bounded
  ring buffer (`collections.deque(maxlen=N)`) per `(namespace, pod_uid,
  container)`, process-lifetime only, explicitly not a time-series database --
  Prometheus is the answer for anyone who actually needs real retained
  history, not a reimplementation of one in SQLite.
- `/analyze` is the only code path that talks to the Kubernetes API for
  optimization purposes; every `GET` reads the repository, never the
  cluster, and reports the `/analyze` run's timestamp as data freshness.

## 12. Testing strategy

- **Unit** (no cluster, no metrics-server, matching `tests/unit/`'s existing
  fully-offline convention): `quantities.py` parse/format round-trips
  (including edge cases: bare integers, `m` suffix, binary vs. decimal
  memory suffixes, scientific notation), `ownership.py` chain resolution
  against hand-built typed objects (same `kubernetes.client` model
  convention already used in `tests/unit/factories.py`), `analyzer.py`
  determinism (same input -> byte-identical output) and each safety-margin
  parameter, `EvidenceQuality` assignment thresholds, expiration logic,
  active-incident flagging against a fake `IncidentRepository`.
- **Integration** (`tests/unit/`-style mocked clients, or `tests/integration/`
  if a real cluster is genuinely needed): Deployment -> ReplicaSet -> Pod
  ownership against mocked `AppsV1Api` responses, metrics-server absent
  (`CustomObjectsApi` raising 404), Prometheus absent/unreachable,
  recommendation persistence + filtering, API validation/error-code
  coverage, a recommendation correctly referencing an existing
  `test_incident_manager.py`-style incident fixture.
- **Live kind-cluster** (`tests/integration/`, same skip-if-unreachable
  fixtures already in `tests/conftest.py`): new lab scenarios --
  oversized-CPU-request, oversized-memory-request-with-data, near-limit
  memory usage, missing-requests-entirely, Pending-due-to-resources (the
  existing `scenario-pending-resources` already demonstrates this one),
  unhealthy-workload-excluded. A live test asserts zero mutating calls were
  made during a real `/analyze` pass against the real cluster (not just
  mocked) -- the strongest form of the "read-only" guarantee.
- Full existing suite re-run after each milestone; any regression is fixed
  before moving on, matching this project's established discipline (see
  TODO.md's Known limitations for the pattern: find it live, document root
  cause, fix, re-verify).

## 13. Milestone plan (this extension)

1. **Repository audit, design doc, baseline** -- this document. Done.
2. Typed models (`models/optimization.py`), `quantities.py`, the
   `MetricsProvider` Protocol + `MetricsApiProvider` (metrics-server) +
   `PrometheusProvider` (optional) + `InMemorySampleStore`.
3. `ownership.py` + `inventory.py` (workload inventory, reusing
   `ClusterClients` extended with `custom_objects`/`batch`).
4. `analyzer.py` + `safety.py` (deterministic CPU/memory analysis, pressure
   detection, safety flags).
5. `persistence/optimization_repo.py`, `api/optimization_routes.py` wired
   into `app.py`, `cli/main.py` commands.
6. Incident integration (read-only lookups into `IncidentRepository`), new
   Prometheus metrics, new RBAC files.
7. Unit + integration tests; live kind lab scenarios where practical.
8. Documentation updates (this file plus README/ARCHITECTURE/
   KUBERNETES_CONCEPTS/RBAC_AND_THREAT_MODEL/DESIGN_DECISIONS/TESTING/
   DEMO_SCRIPT/TODO) and an end-to-end demo.

Each milestone: explain the files touched, implement, run the real test
suite, report actual results, fix regressions before proceeding -- the same
discipline this project has followed since its first milestone.

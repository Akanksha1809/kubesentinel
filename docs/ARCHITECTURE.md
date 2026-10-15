# Architecture

## Pipeline (current, Milestones 1-9)

```
                 +-------------------+
 kube-apiserver  |  Pod/Event/Deploy-|  list-then-watch per resource type, 410 Gone
  (watch/list)   |  ment/PVC/Node/   |  -> relist, periodic resync, exponential
                 |  EndpointSlice    |  backoff on error (observer/watchers.py)
                 |  watchers         |
                 +---------+---------+
                           |
                           v  typed client object (or dict, EndpointSlice only)
                 +-------------------+
                 |  inspector.py     |  pure functions: object -> list[Evidence]
                 |  (observer/)      |  (no network, no side effects)
                 +---------+---------+
                           |
                           v  list[Evidence]
                 +-------------------+
                 | IncidentManager   |  dedup by resource UID (any kind): finds or
                 | (incidents/)      |  opens an Incident, runs the diagnostic
                 +---------+---------+  engine, classifies health, detects recovery
                           |
              +------------+---------------------------+
              v                                         v
   +-------------------+                      +-------------------+
   | DiagnosticEngine  |                      | SQLiteIncidentRepo|  upsert by
   | (diagnostics/)    |                      | (persistence/)    |  incident_id,
   +-------------------+                      +---------+---------+  JSON blob/row
                                                         |
                            +----------------------------+----------------------------+
                            v                                                         v
                 +-------------------+                                     +-------------------+
                 |  FastAPI + CLI    |  read endpoints + token-gated       |  RemediationService|
                 |  (api/, cli/)     |  plan/approve/execute/reject/       |  (remediation/)    |
                 |                   |  dry-run + /metrics                |  propose/approve/  |
                 +-------------------+                                     |  execute/reject/   |
                                                                             |  dry_run           |
                                                                             +----------+----------+
                                                                                        |
                                                              +-------------------------+-------------------------+
                                                              v                         v                         v
                                                   +-------------------+     +-------------------+     +-------------------+
                                                   |  PolicyEngine     | --> | RemediationExecutor| --> | RecoveryVerifier  |
                                                   |  (policy/)        |     | (remediation/)      |     | (verification/)   |
                                                   |  mode/allowlist/  |     | restart/rollback/   |     | polls the live    |
                                                   |  cooldown/budget  |     | scale/recommend-*   |     | cluster, never    |
                                                   +-------------------+     +-------------------+     |  trusts the       |
                                                                                                         |  action's own     |
                                                                                                         |  success          |
                                                                                                         +-------------------+

Prometheus metrics (metrics/) are emitted from every box above with a decision
to record: incidents detected/active, diagnostic/recovery duration, observer
errors, remediation attempts/successes/failures, actions requiring approval --
all bounded-cardinality labels (category/action_type/reason/component), never
an incident ID or resource name. Exposed at GET /metrics; see observability/
for a scrape config and dashboard.
```

## Optimization pipeline (Milestones 12-19)

A second, read-only pipeline, built as its own module tree
(`optimization/`) rather than extended into the pipeline above -- it shares
only the SQLite file and, optionally, `IncidentRepository` (for the
`ACTIVE_INCIDENT` safety flag), never a cluster watch loop or a parallel
incident concept:

```
                 +-------------------+
 kube-apiserver  |  inventory.py     |  one-shot LIST per resource type
  (list only,    |  (optimization/)  |  (Pods/ReplicaSets/Deployments/
  never watch)   |                   |  StatefulSets/DaemonSets/Jobs) --
                 +---------+---------+  no watch loop, no resync timer
                           |
                           v  list[WorkloadSnapshot] (owner chain resolved:
                           |   Pod -> ReplicaSet -> Deployment, or direct
                           |   for StatefulSet/DaemonSet/Job, or standalone)
                 +-------------------+
                 | metrics_providers |  MetricsApiProvider (metrics-server,
                 | .py               |  via CustomObjectsApi) + optional
                 +---------+---------+  PrometheusProvider; InMemorySampleStore
                           |              pools usage across a workload's replicas
                           v  list[RawUsageSample] (honest-absence on failure,
                           |   never a fabricated zero)
                 +-------------------+
                 |  analyzer.py      |  deterministic: percentile+margin for CPU,
                 |  (optimization/)  |  peak+margin for memory, never from <3
                 +---------+---------+  samples/<5min -- plus data-quality/
                           |              scheduling-pressure checks needing no
                           v  list[ResourceRecommendation]  usage data at all
                 +-------------------+
                 |  safety.py        |  flags (never silently drops): unhealthy/
                 |  (optimization/)  |  system-namespace/Job/DaemonSet workloads,
                 +---------+---------+  and an ACTIVE_INCIDENT on the same UID
                           |
                           v
                 +-------------------+
                 | OptimizationService| the only module here with side effects:
                 | (optimization/)    | persists, supersedes prior undecided
                 +---------+----------+ rows, computes effective (incl. EXPIRED)
                           |              status at read time
              +------------+------------+
              v                         v
   +-------------------+     +-------------------+
   |  FastAPI + CLI    |     | SQLiteOptimization|
   |  (api/, cli/)     |     | Repo (persistence/)|
   |  /optimization/*  |     | 3 tables, same db  |
   +-------------------+     +-------------------+
```

No apply/execute endpoint exists anywhere in this pipeline -- see "What's
deliberately not built" below.

## Why this shape

**Evidence is a first-class type, not a side effect of diagnosis.** Every fact a
rule uses is a `kubesentinel.models.evidence.Evidence` object with a `kind`, a
`source_field` naming exactly which part of the Kubernetes API response it came
from, and a `data` dict. Rules never read the raw API object (`V1Pod`,
`V1Deployment`, the `EndpointSlice` dict, ...) -- they only ever see
`list[Evidence]`. This is what makes "distinguish observed facts from inferred
hypotheses" an enforceable property rather than a design aspiration: a rule that
wants to claim something has to point at a specific `Evidence.id` in
`supporting_evidence`, and anything it couldn't verify goes in `missing_evidence`
instead of being silently assumed.

**Diagnostic rules are pure functions** (`list[Evidence] -> list[Hypothesis]`),
with no network calls, no cluster access, no randomness -- this is what makes
the bulk of this project's 218 unit tests run in under two seconds with no
cluster: a rule test just builds `Evidence` (via real `kubernetes.client` model
objects wrapped by `inspector.py`, or plain dicts for the one resource with a
client bug -- see TROUBLESHOOTING.md) and asserts on the `Hypothesis` list.

**Incidents are keyed by resource UID, not name+namespace, and dedup works
across resource kinds, not just Pods.** Kubernetes itself treats UID as the
only stable identity for an object across its lifetime. A Pod *and* its owning
Deployment are tracked as two independent incidents (different UIDs, different
kinds) when both show a failure signal -- `IncidentManager.observe()` doesn't
care what kind a `ResourceRef` is, only its UID, which is also why
`ROLLOUT_FAILED` (Deployment-level) and `CRASH_LOOP_BACKOFF` (Pod-level) can
coexist as separate incidents for the same underlying failure, each citing its
own evidence. See TODO.md's Known limitations for where this still breaks down
(a Deployment rollout creates new Pod UIDs, so a Pod-level incident doesn't
auto-correlate across a rollout fix).

**Diagnosis, policy, remediation, verification, persistence, and transport are
separate layers**, each knowing only about the layer(s) immediately adjacent to
it: `DiagnosticEngine` knows nothing about SQLite or the cluster; `PolicyEngine`
never touches the Kubernetes API, the database, or the clock on its own --
every fact it needs is handed to it in a `PolicyRequest`, which is exactly what
makes every branch of it unit-testable without mocking a cluster;
`RemediationExecutor` is the *only* code in the project permitted to mutate
cluster state, and only through the closed `ActionType` enum's specific
operations; `RecoveryVerifier` is a separate concern from both of them,
confirming an action's effect with a fresh API read rather than trusting the
executor's own "it returned 200" as truth. `RemediationService` is deliberately
where this stops being side-effect-free -- it's the first (and only) module that
wires policy's decision to the executor and verifier. `SQLiteIncidentRepository`
sits behind `persistence/interface.py` specifically so swapping in PostgreSQL
later touches one file, not the manager, policy engine, remediation service,
API, or CLI.

**The watch loop mirrors how Kubernetes informers actually work**, not a
polling loop: LIST to get a resourceVersion baseline, WATCH from that
resourceVersion, treat HTTP 410 (Gone -- the resourceVersion aged out of the
API server's watch cache) as a signal to relist from scratch rather than an
error to just retry, and force a periodic resync even on an otherwise-healthy
stream as a safety net against any missed event. `observer/backoff.py` is
deliberately a pure function so the reconnect math is unit-tested without
spinning up a fake watch stream; `tests/integration/test_watch_resync.py`
additionally confirms the resync boundary actually fires against a real API
server, not just that the math is right.

**The remediation service's three-call lifecycle (`propose`/`approve`/
`execute`) matches the spec's distinct plan/approve/execute operations rather
than collapsing any two of them.** `approve()` deliberately does not execute --
a human can approve now and execute later (or never) -- which is also why
`execute()` exists as its own method rather than being folded into `approve()`.
`dry_run()` is a fourth, side-effect-free sibling: it evaluates the same policy
decision a real `propose()` would, against the real current action history (a
realistic cooldown/budget preview), but builds, persists, and executes nothing.

**Concurrency is handled narrowly, where it was actually shown to matter, not
everywhere defensively.** `propose()`'s read-recent-actions -> decide ->
persist sequence is guarded by a single process-wide lock (found necessary by
an actual concurrency test, not added speculatively -- see TODO.md #20); the
SQLite repository has its own lock around each individual read/write. Nothing
else in this single-process, local-tool design is currently lock-protected,
and that remaining gap (two different callers racing `approve()`/`execute()`/
`reject()` on the same incident) is named explicitly as a known limitation
rather than silently assumed safe.

**AI summarization (`ai/`) sits off to the side of every decision path, never
in it.** `ai/summarizer.py::summarize_incident()` takes an already-built
`Incident` and an `AiClient` and returns text -- it has no reference to
`ClusterClients`, the repository, or `PolicyEngine`, so there is no path from
a model's output back into a diagnosis, a policy decision, or an executed
action, even in principle. `AiClient` is a `Protocol` the same way `Watcher`
is in `observer/watchers.py`: `OllamaClient` (the only implementation) talks
to a local server over plain HTTP, no API key, no outbound network call, no
cost. Disabled by default; every other layer above works fully without it.

**The optimization engine is read-only by construction, not by convention.**
`inventory.py`/`analyzer.py`/`safety.py` never import a Kubernetes client
method that isn't `list_*`/`get_*` -- checked, not just asserted: a live
integration test patches the HTTP transport itself and asserts only `GET`
methods were ever issued during a full `analyze()` pass (inventory +
metrics-server call + persistence). `safety.py` additionally never touches
a repository or the clock on its own -- every fact it flags on (incident
IDs, health, namespace, kind) is already on the `WorkloadSnapshot`/
`Settings` it's handed, the same "pure function over already-fetched
facts" shape as `policy/engine.py::PolicyEngine`. There is no
`ActionType`/executor for resources at all yet: a future "apply" path
would reuse the *existing* `PolicyEngine`/`RemediationExecutor` lifecycle,
not a parallel approval system -- deliberately not built, see
RESOURCE_OPTIMIZATION_DESIGN.md §6.

**CPU and memory right-sizing use different statistics on purpose.** CPU
proposals come from a percentile of *typical* demand (default p95) plus a
safety margin; memory proposals come from observed *peak* (default p99),
never the average -- a transient average is dangerous for a memory limit
in a way it isn't for a CPU request. Both are withheld entirely (the row
still appears, with `reasoning` explaining why) below `LIMITED_HISTORY`
evidence quality -- a configurable minimum sample count and time window,
never a number proposed from a single sample.

**Analysis results are reviewable records with their own lifecycle, not
live cluster state re-read on every request.** `GET` endpoints serve
already-persisted data from the last `analyze()` call; only `POST
/optimization/analyze` itself touches the cluster. A recommendation's
*effective* status (e.g. `EXPIRED` once past `expires_at`) is computed at
read time via `_with_effective_status()`, never by a background job
mutating stored rows on a timer. A later `analyze()` run supersedes a
still-`NEW` prior recommendation for the same workload/container/category,
but never touches one a human has already `REVIEWED`/`APPROVED`/
`REJECTED` -- that decision is a historical record, not something a later
pass gets to silently overwrite.

## What's deliberately not built

- **Multi-namespace watch correctness beyond resync cadence** -- see
  DESIGN_DECISIONS.md.
- **DNS/connectivity diagnosis** -- the spec itself qualifies this category
  ("when the evidence supports it"); passive evidence alone gives too weak a
  signal without an active probe, which is out of scope. See
  DIAGNOSTIC_RULES.md.
- **Unattended auto-remediation beyond a plain restart** -- the policy engine's
  `CONTROLLED_AUTO` mode can only ever auto-execute `RESTART_DEPLOYMENT`, and
  only when the diagnosing rule itself believes a restart will help (which, as
  of every rule implemented today, none of them do -- see
  REMEDIATION_SAFETY.md).
- **AI beyond summarization** -- `ai/summarizer.py` only ever explains an
  already-computed diagnosis in plain English; it never produces a new
  hypothesis, never ranks or re-ranks anything, and has no path to policy or
  remediation. A cloud backend (behind the same `AiClient` Protocol) was
  deliberately not added -- the local Ollama backend is genuinely free, which
  a cloud API, even with a free tier, is not.
- **An apply/execute path for resource recommendations** -- see
  RESOURCE_OPTIMIZATION_DESIGN.md §6; every recommendation today is a
  reviewable record only.
- **Cluster-level overcommitment analysis** (total requested vs. total
  Node-allocatable capacity across every workload) -- considered during
  Milestone 17, but the design doc's own milestone plan never actually
  committed to it; a per-container `ResourceRecommendation` also isn't the
  right shape for a cluster-wide number, so this would need a new field on
  the `/optimization/summary` response, not something bolted onto the
  existing model.
- **A Node-metrics ClusterRole** -- `MetricsApiProvider.get_current_node_usage()`
  has existed since Milestone 13 but has no caller anywhere in this
  codebase; granting RBAC for a capability nothing uses yet would be ahead
  of the code that uses it (see `rbac/optimization-role.yaml`'s own
  docstring).

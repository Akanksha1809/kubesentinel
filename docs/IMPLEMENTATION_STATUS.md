# Implementation status

A capability-by-capability audit against the platform specification's own
section numbering (`docs/PLATFORM_ROADMAP.md`'s source document, sections
2-12). Written at the start of this extension's own Milestone 0, before any
new code -- every "Implemented" claim below names the file(s) that back it
and, where relevant, the test(s) that exercise it, so this document can be
checked against the repository rather than trusted on its own. Updated at
the end of each later milestone, never left to drift from what the code
actually does.

Status legend: **Implemented** (built, tested, live-verified) · **Partial**
(built but narrower than the spec describes, named explicitly) · **Missing**
(not started).

## Baseline test results (this audit)

`./scripts/test.sh`: ruff clean, mypy clean (66 source files), **410 unit
tests passed**, 0 failed.

`./scripts/test.sh --all`: integration/e2e tests are skip-safe by design
(every fixture skips, never fails, when its precondition -- a reachable
cluster, a specific scenario -- isn't met). Run at audit time under this
machine's documented shared-VM contention (a second, unrelated kind cluster
on the same Colima VM was reading 300%+ CPU; see TODO.md's recurring
"Environment note" and Milestone 18/19's writeups for the same pattern
observed repeatedly throughout this project's history): most integration
tests skipped because the session-scoped cluster-connectivity check itself
timed out. This is **not newly broken** -- the full suite (410 unit + 32
integration/e2e = 442 tests) was verified passing cleanly multiple times in
the immediately preceding session, most recently while closing out
Milestone 19 (documentation), each time by re-running the affected tests in
isolation once contention eased and confirming 100% pass. This audit does
not re-litigate that; it's cited here as the last-known-good baseline this
new work builds on, with the honest caveat that a from-scratch `--all` run
on a contended machine will show skips, not failures.

## Core Capability A: Intelligent troubleshooting and self-healing

**Implemented**, the original 11-milestone build (TODO.md Milestones 0-11).

| Spec item | Status | Evidence |
|---|---|---|
| CrashLoopBackOff | Implemented | `diagnostics/rules/crash_loop.py` |
| OOMKilled | Implemented | `diagnostics/rules/oomkilled.py` -- HIGH confidence when the kubelet reports `OOMKilled` directly, MEDIUM inferred-from-exit-code-137 fallback (this dev environment's kubelet doesn't always report the reason; see TROUBLESHOOTING.md) |
| Image pull failures | Implemented | `diagnostics/rules/image_pull.py` |
| Pending Pods | Implemented, narrow | `diagnostics/rules/scheduling.py` -- classifies the scheduler's own `PodScheduled=False` message text (insufficient resources / taint / affinity-selector / unrecognized). Does **not** independently reconstruct *why* -- no Node allocatable math, no affinity-rule evaluation, no taint/toleration matching of its own. See Capability C below, which is exactly the gap this spec asks to fill. |
| Failed Deployment rollouts | Implemented | `diagnostics/rules/deployment.py` (`RolloutFailedRule`), trusts only `ProgressDeadlineExceeded` (a transient `Available=False` during a normal rollout is explicitly not treated as failure -- found live, see DESIGN_DECISIONS.md) |
| Readiness/liveness probe failures | Implemented | `diagnostics/rules/probes.py`, distinguishes the two by Event message text (both report via the same `reason=Unhealthy`) |
| Missing/incorrect Service selectors | Implemented | `diagnostics/rules/service.py` (`ServiceSelectorMismatchRule`), MEDIUM confidence (zero endpoints is consistent with both a selector typo and "no Pods exist yet") |
| Missing EndpointSlice endpoints | Implemented | same rule, fed by `observer/watchers.py::EndpointSliceWatcher` |
| PVC binding problems | Implemented, binding-phase only | `diagnostics/rules/pvc.py` -- diagnoses `ProvisioningFailed`-style binding failure Events. **In-Pod mount failures** (e.g. a Pod stuck in `ContainerCreating` because an already-bound volume won't mount) are not covered -- no rule reads `FailedMount` Events today. |
| Node pressure / unhealthy Nodes | Implemented | `diagnostics/rules/node.py` (`NodeConditionRule`) |
| Missing ConfigMaps/Secrets | Implemented | `diagnostics/rules/missing_config.py`, reads the kubelet's own `CreateContainerConfigError` message (never reads Secret values -- see RBAC_AND_THREAT_MODEL.md) |
| DNS and connectivity problems | **Missing**, deliberately deferred | `ReasonCode.DNS_RESOLUTION_FAILURE` exists in `models/diagnosis.py` as a reserved value, but no rule produces it and it is not in `diagnostics/engine.py::DEFAULT_RULES`. The original spec's own "when the evidence supports it" qualifier was read as "passive evidence alone is too weak a signal without an active probe," which is exactly what Capability D (section 3 of this document) is scoped to build. |
| Resource requests/limits contributing to failures | Partial, by design split | The troubleshooting engine itself does not diagnose this; Capability B (below) does, as a separate read-only analysis. Nothing currently cross-links an optimization finding (e.g. "this container has no memory limit") into an open incident's own hypothesis list -- they are two independent outputs today, linked only one direction (`ACTIVE_INCIDENT` on a recommendation), not the other. |

Diagnosis shape: every `Hypothesis` carries `confidence` (never "proven"),
`supporting_evidence`/`missing_evidence` (explicit, not implied),
`recommended_next_steps` -- confirmed by `models/diagnosis.py` and every
rule above. Deterministic rules are the only decision-maker; the optional
AI layer (`ai/`) only summarizes an already-computed `DiagnosisResult` in
plain English and has no reference to `ClusterClients`, the repository, or
`PolicyEngine` (confirmed in RBAC_AND_THREAT_MODEL.md's trust-boundary
section) -- it cannot invent an observation or authorize anything.

Remediation: precondition validation, audit trail (`Incident.audit_log`),
action limits (cooldown + per-incident/per-window budgets), the shared
policy layer (`PolicyEngine`), and post-action verification
(`RecoveryVerifier`, which polls the live cluster rather than trusting an
action's own reported success) are all implemented -- see
REMEDIATION_SAFETY.md for the full enforcement list and live-verification
evidence.

## Capability B: Resource Optimization Engine

**Implemented**, this project's own prior extension (TODO.md Milestones
12-19, `docs/RESOURCE_OPTIMIZATION_DESIGN.md`).

| Spec item | Status | Evidence |
|---|---|---|
| CPU/memory usage vs. requests/limits | Implemented | `optimization/analyzer.py` |
| Overprovisioning / resource pressure detection | Implemented | `CPU_OVERSIZED`/`MEMORY_OVERSIZED` (waste) and `CPU_PRESSURE`/near-limit (pressure) are structurally distinct checks |
| CPU/memory right-sizing recommendations | Implemented | percentile-of-typical for CPU (default p95), peak for memory (default p99), never the average for either |
| Historical peaks/percentiles when available | Implemented | `optimization/metrics_providers.py::PrometheusProvider` (optional, `container_cpu_usage_seconds_total`/`container_memory_working_set_bytes`); without it, `InMemorySampleStore` pools metrics-server snapshots across repeated `analyze()` calls within one process's lifetime |
| Sample count / duration / freshness / health / confidence | Implemented | `UsageSummary.sample_count`/`window_seconds`/`evidence_quality`, `WorkloadSnapshot.healthy` -- no proposed number below `LIMITED_HISTORY` (configurable minimum: 3 samples, 5 minutes) |
| Missing requests/limits | Implemented | `DATA_QUALITY` category |
| OOM risk | Implemented | `recent_oom_detected` flag -> `OOM_HISTORY`-flagged `MEMORY` recommendation even with zero other usage evidence, and vetoes any proposed *decrease* |
| CPU throttling | **Partial -- scaffolded, not populated** | `UsageSummary.throttled` field exists but is hardcoded `None` everywhere it's constructed (`analyzer.py` lines ~113, ~164); the near-limit `risks` text is inferred from proximity to the configured limit, not from an actual `container_cpu_cfs_throttled_seconds_total` query. `PrometheusProvider.query_range()` never queries that metric. A real gap, not a documentation inaccuracy -- this is exactly the kind of claim this audit exists to catch rather than restate uncritically. |
| Resource-related scheduling problems | Implemented | `SCHEDULING` category, reusing `diagnostics/rules/scheduling.py`'s message classification |
| Distinguish inefficiency from spikes/defects | Implemented | peak-not-average for memory, percentile-not-max for CPU, `LIMITED_HISTORY` threshold before any number |
| Explain every recommendation | Implemented | `reasoning` is always populated with the actual observed statistic vs. the actual configured value |
| Preserve original config | Implemented, trivially -- there is no apply path | Analysis never mutates `spec.template.spec.containers[].resources`; a future apply milestone would need to snapshot the original explicitly, not yet built |
| Read-only by default | Implemented, checked not just asserted | A live integration test patches the HTTP transport itself and asserts a full `analyze()` pass (inventory + metrics-server + persistence) issues only `GET` |
| No aggressive downsizing from one sample | Implemented | `EvidenceQuality` gating, unit- and live-tested against real metrics-server data (Milestone 18) |
| No automatic limit increase from one OOM event | Implemented | `OOM_HISTORY` flags and vetoes a *decrease*; it never proposes an *increase* either -- the row simply carries no safe proposed number either direction until there's enough evidence |

Workload-kind handling: Deployments/StatefulSets/standalone Pods are fully
supported; Jobs and DaemonSets are deliberately treated conservatively
(`JOB_CONSERVATIVE`/`DAEMONSET_CONSERVATIVE` flags suppress any proposed
number, never silently).

**Deliberately not built**, named explicitly rather than left unstated:
cluster-level overcommitment analysis (needs Node allocatable capacity
aggregated across every workload -- doesn't fit today's per-container
`ResourceRecommendation` shape); a Node-metrics ClusterRole (nothing calls
`MetricsApiProvider.get_current_node_usage()`, which has existed since this
engine's own Milestone 2 but has no caller); any apply/execute path (see
"Shared policy layer" below).

## Capability C: Kubernetes Scheduling & Placement Advisor

**Implemented** (Milestone 3 of `docs/PLATFORM_ROADMAP.md`;
`docs/SCHEDULING_ANALYSIS.md` is the design). A new `scheduling/` package:
`node_inventory.py` (Node allocatable/requested/taints/labels, built the
same one-shot-snapshot way `optimization/inventory.py` is), `constraints.py`
(pure per-constraint functions), `analyzer.py` (orchestrates every
constraint against every Node for a real or hypothetical Pod spec),
`service.py` (the one module with side effects, mirroring
`OptimizationService` exactly).

| Spec item | Status | Evidence |
|---|---|---|
| Node allocatable CPU/memory | Implemented | `RESOURCE_FIT`, reproducing the scheduler's own accounting (allocatable minus every *other* bound Pod's own requests) |
| Node selectors | Implemented | `NODE_SELECTOR`, exact label-equality |
| Node affinity (required) | Implemented | `REQUIRED_NODE_AFFINITY`, all 6 real operators (`In`/`NotIn`/`Exists`/`DoesNotExist`/`Gt`/`Lt`), terms OR'd, expressions within a term AND'd |
| Node affinity (preferred) | Detected, not evaluated | a soft scoring preference -- reported in `unevaluated_constraints`, never treated as blocking |
| Pod affinity / anti-affinity | Detected, not evaluated | needs every other Pod's labels across every candidate node -- out of v1 scope, named explicitly in every finding that has one |
| Taints and tolerations | Implemented | `TAINT_TOLERATION` -- real effect/operator/value matching; `PreferNoSchedule` reported as soft, never blocking, matching real scheduler semantics |
| Topology spread constraints | Detected, not evaluated | same reasoning as pod affinity |
| PVC binding / volume topology | **Not built** | not in this milestone's scope; the troubleshooting engine's own `PvcBindingFailureRule` already covers binding-failure diagnosis separately |
| Unschedulable Node conditions | Implemented | `NodeSnapshot.ready`/`unschedulable` -- a cordoned or not-Ready Node is never schedulable, before any constraint is even checked |
| Pod priority / preemption | **Not built** | explicitly out of v1 scope (real preemption victim-selection is well beyond "explain and simulate") |
| Namespace quotas | Implemented, hypothetical-spec only | `NAMESPACE_QUOTA` -- deliberately **not** checked for a real, already-existing Pod (it already passed admission, so quota can't explain its Pending state); only meaningful for `POST /scheduling/simulate`'s hypothetical path, and the code enforces that distinction rather than checking it unconditionally |
| Admission constraints generally | Partial | only ResourceQuota; no LimitRange, no custom admission webhook awareness |
| Explain blocking constraints | Implemented | per-node, per-constraint `ConstraintResult` with real arithmetic/labels in `reasoning`, not a generic message |
| Hard vs. soft constraints distinguished | Implemented | `PreferNoSchedule`/preferred node affinity never set `satisfied=False`; the finding's `unevaluated_constraints` list is itself the hard/soft-equivalent "evaluated vs. detected" distinction for the constraints this engine doesn't score |
| Observed vs. inferred evidence separated | Implemented | a real Pod's `scheduler_message` (the scheduler's own words) is carried alongside this engine's own independent math -- never conflated, both shown |
| Read-only placement simulation | Implemented | `POST /scheduling/simulate`, a raw Pod spec dict deserialized into a typed `V1PodSpec` via the Kubernetes client's own deserializer, analyzed with zero mutation |
| No replacement scheduler | Honored | no scoring/ranking logic anywhere -- pass/fail per constraint only, explicitly not a reimplementation of `kube-scheduler`'s own plugins |

**A real, checked-not-assumed approximation, named here**: resource-fit math
sums only regular containers' requests, not init containers (the real
scheduler takes the *max* of init vs. regular container requests per
resource, not a sum) -- documented in `node_inventory.py::_pod_requested`'s
own docstring, not silently wrong.

**A real gap caught live during this milestone's own verification, by this
audit's own standard**: `SchedulingFinding.related_incident_ids` was
declared in the design doc and the model from the start, but
`SchedulingService` had no `incident_repo` parameter at all until a live
check against the real cluster showed the field staying empty on every
finding -- the exact "scaffolded but never populated" shape this document
flagged for `UsageSummary.throttled` in the optimization engine. Fixed
within the same milestone, before it shipped: `SchedulingService._open_incident_ids_for()`
now does the same dual-UID lookup `OptimizationService` already does, and a
live check against a real `pending-resources-demo` Pod with a genuinely
open incident confirmed the link now appears.

**Environment constraint, confirmed still true**: the lab kind cluster
remains single-node. The analyzer itself loops over N nodes correctly
(confirmed by `test_multiple_nodes_schedulable_if_any_one_fits`, a unit
test with two synthetic nodes), but the live demo can only ever show one
node's own pass/fail, never a genuine multi-node trade-off. Not addressed
in this milestone -- still a legitimate future addition if a multi-node
demo becomes valuable.

**Live-verified against the real cluster**: the existing
`pending-resources-demo` scenario correctly blocks on `RESOURCE_FIT` alone;
a new `invalid-node-selector-demo` scenario (added this milestone, see
FAILURE_LAB.md) correctly blocks on `NODE_SELECTOR` alone, with
`RESOURCE_FIT` passing -- confirming the engine distinguishes *which*
constraint blocked placement, not just that scheduling failed. Both
confirmed via real node allocatable/requested numbers read live (4000m CPU,
~5.1Gi memory allocatable; ~2150m/~950Mi already requested by other lab
Pods at analysis time), not mocked. A live integration test
(`tests/integration/test_scheduling_live.py`) patches the HTTP transport
itself and confirms a full `analyze_pod()` pass issues only `GET`.

## Capability D: Kubernetes Network Troubleshooting Engine

**Missing.** No `network/` module, no `NetworkingV1Api` client on
`ClusterClients` (no NetworkPolicy/Ingress access today).

What exists and is reusable: `observer/watchers.py::EndpointSliceWatcher` +
`inspector.py`'s Service/EndpointSlice evidence extraction (already proven
live, via `ServiceSelectorMismatchRule`) -- a real foundation for the
Service -> EndpointSlice half of the request-path explanation this
capability wants to build.

**Environment constraint worth stating now**: this project's kind cluster
uses kind's default CNI (kindnet), confirmed via `lab/kind-config.yaml`
(no `networking.disableDefaultCNI` override). **kindnet does not enforce
NetworkPolicy at all.** This means any `NetworkPolicy`-related analysis
built on this lab cluster can only ever be *static* (reading the policy
object's selectors/ports/direction and reasoning about what it *should*
restrict) -- it can never be confirmed against actual observed blocked
traffic, because nothing on this cluster is actually enforcing the policy.
The design for this capability must say this explicitly wherever it reports
a NetworkPolicy-related finding (matching the spec's own instruction: "Do
not claim a NetworkPolicy blocks traffic unless the available evidence
supports that conclusion") -- on this lab setup, the evidence can only ever
support "this policy, if enforced, would restrict X," never "X is actually
blocked." A CNI swap (e.g. Calico) is the only way to close that gap, and is
out of scope unless explicitly requested.

No DNS diagnostic capability exists at all yet (Capability A's own
`DNS_RESOLUTION_FAILURE` reason code is unused) -- this capability is where
that gap would actually get closed, via a bounded, opt-in active probe
(the spec's own requirement), not passive evidence alone.

## Capability E: Capacity Planning & Autoscaling Advisor

**Missing.** No `AutoscalingV2Api` client on `ClusterClients` (no HPA
access today). The spec's own instruction to "extend the resource
optimization engine rather than creating a duplicate metrics collector" is
directly actionable: `optimization/metrics_providers.py` and
`InMemorySampleStore` are the natural, already-built place to source the
usage history this capability needs: no second collector should be built.

## Capability F: Kubernetes Security & Configuration Auditor

**Missing.** No `security/` module, no `RbacAuthorizationV1Api`/`PolicyV1Api`
client on `ClusterClients`, no rule catalog, no integration with an
external scanner (kube-linter/Kubescape -- neither is installed or
referenced anywhere in this repo today, confirmed by grep).

## Shared policy, approval, and remediation layer

**Implemented for Capability A only; Capability B deliberately bypasses it
in a way worth naming precisely before C/D/E/F are built.**

`policy/engine.py::PolicyEngine` gates every troubleshooting-engine
mutation (`propose()` -> `evaluate()` before anything is built or
persisted) with mode (OBSERVE/APPROVAL_REQUIRED/CONTROLLED_AUTO),
namespace/resource-name allowlists, cooldowns, and per-incident/per-window
budgets -- see REMEDIATION_SAFETY.md for the full, live-verified list.

The resource optimization engine's `review()` (approve/reject/mark-reviewed
a recommendation) does **not** go through `PolicyEngine` at all -- it has
no `ActionType`, no mode check, no cooldown, no budget. This was a
deliberate Milestone 16 design choice (a review is a decision *record*,
never a cluster mutation, so the troubleshooting engine's mutation-specific
gates don't apply) but it means **when Capability B eventually grows an
apply path**, that path must be built to go through `PolicyEngine`
explicitly -- it will not inherit any policy enforcement automatically just
by existing in this codebase. The same will be true for whatever
remediation surface C/D/E/F eventually need (if any): nothing about
`PolicyEngine`'s current shape auto-applies to a new `ActionType` without
each new executor method explicitly calling into it, the same way
`RemediationExecutor` does today.

Already satisfied for *any* future mutation by construction, not by
policy alone: no cluster-admin RBAC anywhere (`rbac/*.yaml` is entirely
least-privilege, additive, and today 100% read-only outside the one
`remediation-role.yaml`); no arbitrary shell/command execution anywhere in
this codebase (confirmed by the complete absence of `subprocess`/`os.system`
calls outside test fixtures and the one `kubectl create token` test helper);
AI output has no path back to any decision (confirmed above).

## Shared observation, storage, and API

**Implemented, and now proven to scale to a third analysis engine, not just
a second** -- Capability C (`scheduling/`) followed the exact same pattern
Capability B established: a new `SQLiteSchedulingRepository` sibling behind
`persistence/interface.py`, a new `APIRouter` (`api/scheduling_routes.py`),
new CLI commands, zero new watch loops, zero new database files, zero new
web apps. Two data points generalizing to a third without needing to bend
the pattern is a stronger signal than two alone -- `MASTER_ARCHITECTURE.md`'s
own "the one pattern, stated once" section was written after only two
engines existed; it held up unchanged for the third.

API path convention: this repository has never used an `/api/v1` prefix
(`/incidents`, `/optimization/...`, `/metrics` are all flat). The spec's
suggested `/api/v1/<capability>` paths should be read as
`/<capability>/...`, matching `docs/RESOURCE_OPTIMIZATION_DESIGN.md` §8's
own precedent, not the spec's literal path text.

## Kubernetes-native Operator and Reconciliation Controller

**Missing.** No CRDs, no controller process, no `kubebuilder`/`kopf`/similar
dependency anywhere in `pyproject.toml`. Explicitly scoped by the spec as a
later, independently-testable milestone, after the core analysis engines
exist for it to expose as policy -- correctly sequenced last.

## Controlled Chaos & Resilience Lab

**Partial.** The *scenario* half is strong and already live-verified: 12
lab scenario directories under `lab/manifests/` (9 original troubleshooting
scenarios + 2 resource-optimization scenarios + 1 scheduling-advisor
scenario), each with setup
instructions, expected symptoms, expected diagnosis, and cleanup --
documented per-scenario in FAILURE_LAB.md. `lab/scripts/run-scenario.sh`
and `lab/scripts/cleanup.sh` are generic, scenario-agnostic appliers, and
every scenario is scoped to the single `kubesentinel-lab` namespace (no
scenario targets anything outside it).

What's missing is the **evaluation harness** the spec asks for: nothing in
this codebase currently *measures* (and persists, for later comparison)
time-to-detection, time-to-diagnosis, recovery time, or false-positive rate
across a scenario run. Today these numbers only exist as one-off, manually
observed figures written into TODO.md's prose during live verification
(e.g. "the fixed version took 11.5s" from an early milestone) -- real, not
fabricated, but not a repeatable, automated measurement a later run could
reproduce or compare against.

## Observability

**Implemented for Capabilities A, B, and C**, following one consistent,
bounded-cardinality labeling discipline across all three (`category`,
`action_type`, `reason`, `component`, `status`, `provider`, `result` --
never an incident ID, recommendation ID, finding ID, or Pod name as a
label; confirmed by reading every `Counter`/`Gauge`/`Histogram` definition
in `metrics/registry.py`). `observability/prometheus.yml` +
`grafana-dashboard.json` exist and are verified against real `/metrics`
output. No per-module "degraded / unknown / not applicable" distinction
exists as a first-class concept yet (today a missing metrics provider is
communicated via a `warning` string + `metrics_available: false` on the
optimization engine's own summary, not a shared three-state model other
future capabilities could reuse directly) -- worth deciding explicitly
before C/D/E/F each invent their own version of the same idea.

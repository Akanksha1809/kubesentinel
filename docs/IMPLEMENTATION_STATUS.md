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
| PVC binding problems | Implemented | `diagnostics/rules/pvc.py` -- diagnoses `ProvisioningFailed`-style binding failure Events. **In-Pod mount failures** (e.g. a Pod stuck in `ContainerCreating` because an already-bound volume won't mount) are now also covered (closed as part of Milestone 1's own audit, item 2): `diagnostics/rules/mount.py::FailedMountRule` reads the kubelet's own `FailedMount` Warning Event, with `"FailedMount"` also added to `incidents/health.py::_BAD_EVENT_REASONS` so a Pod stuck this way actually opens an incident at all (no container waiting/terminated state ever reflects this failure mode -- the Event is the only evidence it produces). Live verification against the real `ObserverService` (not just the one-shot `diagnose` CLI, which never fetches Events) caught a second, independent bug of the same shape: `observer/service.py::_RELEVANT_EVENT_REASONS` is a separate Event-reason allowlist gating the `EventWatcher` itself, upstream of `_BAD_EVENT_REASONS` -- it also needed `"FailedMount"` added, or the real watcher dropped the Event before `IncidentManager.observe()` ever ran. Fixed and regression-tested; the real Pod incident now opens and diagnoses `FAILED_MOUNT` (HIGH confidence) end to end. |
| Node pressure / unhealthy Nodes | Implemented | `diagnostics/rules/node.py` (`NodeConditionRule`) |
| Missing ConfigMaps/Secrets | Implemented | `diagnostics/rules/missing_config.py`, reads the kubelet's own `CreateContainerConfigError` message (never reads Secret values -- see RBAC_AND_THREAT_MODEL.md) |
| DNS and connectivity problems | Partial, by design split | `ReasonCode.DNS_RESOLUTION_FAILURE` exists in `models/diagnosis.py` as a reserved value, but no rule produces it and it is not in `diagnostics/engine.py::DEFAULT_RULES` -- this engine still has no DNS-resolution diagnosis. Capability D (below) now implements real, live CoreDNS/`kube-dns` *health* checking (`DnsHealthResult`), but deliberately as its own separate model/pipeline, not by finally producing this reason code -- doing so would need the bounded active probe Capability D's own design pass explicitly deferred (see below), not passive health evidence alone. |
| Resource requests/limits contributing to failures | Partial, by design split | The troubleshooting engine itself does not diagnose this; Capability B (below) does, as a separate read-only analysis. The two are now linked in **both** directions (closed as part of Milestone 1's own audit, item 3): a recommendation still carries `ACTIVE_INCIDENT`/`related_incident_ids` pointing at an open incident for the same workload, and -- new -- an `OOM_KILLED` hypothesis now carries `Hypothesis.related_recommendation_ids` pointing back at any open `MEMORY`/`DATA_QUALITY` recommendation for that same workload (e.g. "this container has no memory limit configured, per recommendation `<id>`"), populated by `IncidentManager._enrich_with_recommendations` -- deliberately narrow for its first case (only `OOM_KILLED`, the roadmap's own named example), not every reason code. |

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
| CPU throttling | **Implemented** (closed as part of Milestone 1's own audit, item 1; was "scaffolded, not populated" at the original audit) | `analyzer.py::_check_throttled()` queries real `container_cpu_cfs_throttled_seconds_total` via Prometheus when configured; a confirmed result states the fact explicitly (distinct from the merely hypothetical near-limit phrasing used when no signal is available) and vetoes a proposed CPU decrease, the same discipline already applied to a recorded OOM kill. `UsageSummary.throttled` stays `None` (never coerced to `False`) on any query failure or when only metrics-server evidence is available -- metrics-server itself has no throttling data at all. Unit-tested against a mocked Prometheus response, the same tier every Prometheus-dependent path in this project uses (Prometheus itself has never been deployed in this project's own lab). |
| Resource-related scheduling problems | Implemented | `SCHEDULING` category, reusing `diagnostics/rules/scheduling.py`'s message classification |
| Distinguish inefficiency from spikes/defects | Implemented | peak-not-average for memory, percentile-not-max for CPU, `LIMITED_HISTORY` threshold before any number |
| Explain every recommendation | Implemented | `reasoning` is always populated with the actual observed statistic vs. the actual configured value |
| Preserve original config | Implemented | `ResourceRecommendation.current_requests`/`current_limits` already record the pre-change values as evidence; the apply path itself (docs/RESOURCE_OPTIMIZATION_DESIGN.md §14) patches only the specific keys actually proposed via a merge PATCH, never touching an untouched dimension. No automated undo/rollback for `ADJUST_RESOURCES` specifically (unlike `ROLLBACK_DEPLOYMENT`'s real revision history) -- a human reverting would re-apply the recorded `current_requests`/`current_limits` manually or via a fresh recommendation, named here as a real v1 boundary, not silently assumed covered |
| Read-only by default | Implemented, checked not just asserted | A live integration test patches the HTTP transport itself and asserts a full `analyze()` pass (inventory + metrics-server + persistence) issues only `GET` |
| No aggressive downsizing from one sample | Implemented | `EvidenceQuality` gating, unit- and live-tested against real metrics-server data (Milestone 18) |
| No automatic limit increase from one OOM event | Implemented | `OOM_HISTORY` flags and vetoes a *decrease*; it never proposes an *increase* either -- the row simply carries no safe proposed number either direction until there's enough evidence |
| Cluster-level overcommitment analysis | **Implemented** (closed as later, separate follow-up work; was named "deliberately not built" at the original audit) | `optimization/overcommitment.py::compute_cluster_overcommitment()` -- a new top-level `ClusterOvercommitmentSummary`, never a `ResourceRecommendation` (it's cluster-wide, not per-container). Reports requested-vs-allocatable (should never exceed 1.0; a scheduler-accounting anomaly if it does) and limits-vs-allocatable (never admission-enforced, flagged past a configurable threshold, default 1.0) separately, per Node and cluster-wide, reusing the exact `NodeSnapshot`s already fetched in `analyze()` for the capacity-headroom check (zero new cluster read for these two). A container with no limit at all is tracked via `unlimited_cpu_container_count`/`unlimited_memory_container_count` (new fields on `NodeSnapshot`, extended onto the same per-Node, per-Pod loop that already computes `requested`) -- never folded into the limits sum as if it contributed 0, since a missing limit is a real, unbounded ceiling. See `RESOURCE_OPTIMIZATION_DESIGN.md` §15.1-§15.4. |
| Node-metrics ClusterRole / real Node usage | **Implemented** (closed in the same follow-up as the row above; was named "still deliberately not built" immediately after that row's own original closure) | `MetricsApiProvider.get_current_node_usage()` -- unused since this engine's own Milestone 2 -- now has both a caller (`OptimizationService.analyze()`, the same pass that fetches `node_snapshots`) and a grant for the cluster-scoped `metrics.k8s.io/nodes` resource (new `rbac/optimization-clusterrole.yaml`/`-clusterrolebinding.yaml`, the fourth documented cluster-scope exception in this project). Feeds a *third*, independent ratio onto `NodeOvercommitment`/`ClusterOvercommitmentSummary` -- real, currently-measured usage vs. allocatable, the actionable complement to the two configuration-based ratios above, flagged separately as `under_pressure_cpu`/`under_pressure_memory` past `optimization_node_pressure_ratio_threshold` (default 0.9) since "overcommitted on paper" and "under real pressure" are not the same claim. `None` (never 0) on every actual-usage field whenever no metrics-server is reachable. See `RESOURCE_OPTIMIZATION_DESIGN.md` §15.5. |

Workload-kind handling: Deployments/StatefulSets/standalone Pods are fully
supported; Jobs and DaemonSets are deliberately treated conservatively
(`JOB_CONSERVATIVE`/`DAEMONSET_CONSERVATIVE` flags suppress any proposed
number, never silently).

**Nothing left deliberately unbuilt in this capability** as of this
closure -- both items this section used to name ("cluster-level
overcommitment analysis" and "a Node-metrics ClusterRole") are now
Implemented, above. The apply/execute path named here as deferred in
this milestone's own original scope is also closed, as earlier,
separate, explicitly-requested follow-up work -- see
`RESOURCE_OPTIMIZATION_DESIGN.md` §14, TODO.md's own later milestone
entries for each, and "Shared policy layer" below for how the apply
path fits that layer rather than duplicating it.

**Live-verified against the real cluster, not just unit-tested**: ran
`kubesentinel optimize`/`optimization-summary` and a real `GET
/optimization/summary` against the actual `kind-kubesentinel` cluster.
Confirmed real numbers (4000m CPU / ~4.79Gi memory allocatable, ~960m CPU
/ ~306Mi memory requested, ~150m CPU / ~422Mi memory in specified limits
on the one real Node) and, genuinely found rather than assumed: 8 of the
real containers currently running have no CPU limit and 6 have no memory
limit, both correctly carried through as `unlimited_*_container_count`
rather than silently deflating the ratio. Both configuration ratios land
below the default 1.0 threshold, so `overcommitted_cpu`/`overcommitted_memory`
both correctly read `False` today -- an honest negative result from real
cluster data. The real-usage ratio was confirmed negative in a different,
more fundamental way: this lab genuinely has no metrics-server installed
at all (`kubectl auth can-i list nodes.metrics.k8s.io` itself reports the
API group isn't recognized), so every `actual_*`/`cluster_actual_*` field
correctly reads `None`, never `0` -- the honest-absence path, confirmed
live; the populated-data path (a real sample matched, a ratio computed,
the pressure threshold tripped) is confirmed by unit test instead, named
here explicitly rather than implied by a live run that couldn't actually
exercise it.

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
| Pod affinity / anti-affinity (required) | Implemented | `REQUIRED_POD_AFFINITY`/`REQUIRED_POD_ANTI_AFFINITY` -- terms ANDed, evaluated against every other already-scheduled Pod's labels via a dedicated `build_pod_label_inventory()` snapshot; a required term using `namespaceSelector` can't be evaluated (would need a separate per-Namespace-labels call) and is conservatively treated as not-satisfied for affinity / satisfied for anti-affinity (never guess a required condition holds; never fabricate a violation), and also surfaced in `unevaluated_constraints` |
| Pod affinity / anti-affinity (preferred) | Detected, not evaluated | a soft scoring preference, same treatment as preferred node affinity |
| Taints and tolerations | Implemented | `TAINT_TOLERATION` -- real effect/operator/value matching; `PreferNoSchedule` reported as soft, never blocking, matching real scheduler semantics |
| Topology spread constraints | Implemented | `TOPOLOGY_SPREAD` -- real per-domain skew calculation (`maxSkew`/`topologyKey`/`labelSelector`) including the candidate Pod hypothetically placed; `whenUnsatisfiable=ScheduleAnyway` is soft/non-blocking, mirroring `PreferNoSchedule`; `minDomains`/`matchLabelKeys`/`nodeAffinityPolicy`/`nodeTaintsPolicy` not evaluated, a documented v1 simplification |
| PVC binding / volume topology | **Not built** | not in this milestone's scope; the troubleshooting engine's own `PvcBindingFailureRule` already covers binding-failure diagnosis separately |
| Unschedulable Node conditions | Implemented | `NodeSnapshot.ready`/`unschedulable` -- a cordoned or not-Ready Node is never schedulable, before any constraint is even checked |
| Pod priority / preemption | **Not built** | explicitly out of v1 scope (real preemption victim-selection is well beyond "explain and simulate") |
| Namespace quotas | Implemented, hypothetical-spec only | `NAMESPACE_QUOTA` -- deliberately **not** checked for a real, already-existing Pod (it already passed admission, so quota can't explain its Pending state); only meaningful for `POST /scheduling/simulate`'s hypothetical path, and the code enforces that distinction rather than checking it unconditionally |
| Admission constraints generally | **Implemented for LimitRange** (closed as later, separate follow-up work; was "Partial -- only ResourceQuota; no LimitRange" at the original audit), still **Partial** for custom admission webhooks | `LIMIT_RANGE` -- each container's explicit requests/limits checked against every `type: Container` LimitRange item's `min`/`max` in the namespace, same hypothetical-spec-only framing as `NAMESPACE_QUOTA` just above. `default`/`defaultRequest` injection is deliberately not modeled (a container specifying neither is never flagged just because a `min` exists); `type: Pod`/`type: PersistentVolumeClaim` items and `maxLimitRequestRatio` are not evaluated -- all named explicitly, not silently ignored. No custom admission webhook awareness remains out of scope (this project has no way to introspect a third-party webhook's own logic). See `docs/SCHEDULING_ANALYSIS.md` §12. |
| Explain blocking constraints | Implemented | per-node, per-constraint `ConstraintResult` with real arithmetic/labels in `reasoning`, not a generic message |
| Hard vs. soft constraints distinguished | Implemented | `PreferNoSchedule`/preferred node affinity never set `satisfied=False`; the finding's `unevaluated_constraints` list is itself the hard/soft-equivalent "evaluated vs. detected" distinction for the constraints this engine doesn't score |
| Observed vs. inferred evidence separated | Implemented | a real Pod's `scheduler_message` (the scheduler's own words) is carried alongside this engine's own independent math -- never conflated, both shown |
| Read-only placement simulation | Implemented | `POST /scheduling/simulate`, a raw Pod spec dict deserialized into a typed `V1PodSpec` via the Kubernetes client's own deserializer, analyzed with zero mutation |
| No replacement scheduler | Honored | no scoring/ranking logic anywhere -- pass/fail per constraint only, explicitly not a reimplementation of `kube-scheduler`'s own plugins |

**Closed as later, separate follow-up work -- was a named approximation
at the original audit**: resource-fit math used to sum only regular
containers' requests, ignoring init containers entirely. Now
`node_inventory.py::effective_pod_resource_quantity()` implements the
real scheduler's own algorithm -- `max(sum of regular-container
requests, largest single init-container request)`, since init
containers run one at a time and never concurrently with each other or
the regular containers -- shared by all three call sites that ask this
same question (`_pod_requested`/`_pod_limits` for every other
already-bound Pod on a Node, `analyzer.py::_pod_spec_request` for the
candidate Pod), so the candidate's own math and its neighbors' math can
never diverge again. `restartPolicy: Always` init containers ("native
sidecars," Kubernetes 1.28+) are still treated as ordinary
non-restarting ones -- a further, explicitly named v1 simplification.
See `docs/SCHEDULING_ANALYSIS.md` §13.

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

**Environment constraint on the *primary* cluster, confirmed still
true**: the lab's primary kind cluster remains single-node
(`lab/kind-config.yaml` unchanged). The analyzer itself loops over N
nodes correctly (confirmed by `test_multiple_nodes_schedulable_if_any_one_fits`,
a unit test with two synthetic nodes) -- what was missing was
anywhere to *show* that live. Closed as later, separate, optional
infrastructure: `lab/kind-config-multi-node.yaml` + `lab/create-
multi-node-cluster.sh`/`-delete-multi-node-cluster.sh`, a second,
separate `kubesentinel-multi` cluster (1 control-plane + 2 workers)
that never touches the primary one. Live-verified: a real `POST
/scheduling/simulate` call against the real multi-node cluster, with
exactly one of its two workers labeled `disktype=ssd`, correctly
reported that labeled worker schedulable and the *other* real worker
blocked specifically by `NODE_SELECTOR` -- a genuine cross-node
trade-off against three real, different Nodes, the exact thing this
constraint used to say couldn't be shown. See
`docs/SCHEDULING_ANALYSIS.md` §11. Cluster deleted immediately after
verifying; the primary single-node cluster and every other test/demo
in this project are unaffected.

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

**Live-verified again for the LimitRange closure above**: applied a real
`LimitRange` (`min: {cpu: 200m, memory: 64Mi}`, `max: {cpu: 2, memory:
1Gi}`) to the real `kubesentinel-lab` namespace. A real `POST
/scheduling/simulate` call with a container requesting `cpu: 50m` (below
the real `min`) correctly returned `any_node_schedulable: false`, naming
the exact violation; a second call with a container inside the real
bounds correctly returned `true` -- both directions confirmed against
real cluster state. Cleaned up afterward.

**Live-verified again for the init-container resource-fit closure
above, isolating the exact delta**: applied a real Pod with a 10m-cpu
regular container and a 700m-cpu init container to the real
`kubesentinel-lab` namespace. Measured the real Node's own "already
requested" total via `RESOURCE_FIT`'s own reasoning twice -- once with
the Pod absent (950m) and once present (1650m) -- isolating this one
Pod's contribution as exactly 700m: the init container's real request,
neither the regular container's 10m alone (what every call site read
before this fix) nor a naive 710m sum. Re-measured a second time to
confirm reproducibility, then cleaned up.

## Capability D: Kubernetes Network Troubleshooting Engine

**Implemented** (Milestone 4 of `docs/PLATFORM_ROADMAP.md`;
`docs/NETWORK_DIAGNOSTICS.md` is the design). A new `network/` package:
`service_inventory.py` (Service selector-vs-Pod-label matching, computed
independently rather than inferred from EndpointSlice counts; the
EndpointSlice-vs-selector consistency check; numeric-vs-named `targetPort`
resolution), `policy_inventory.py` (applicable NetworkPolicy summaries),
`dns_health.py` (CoreDNS/`kube-dns` health, a narrow, explicitly-justified
`kube-system` RBAC exception), `analyzer.py` (pure classification over
pre-computed evidence), `probe.py` (the active connectivity probe,
Section 12, opt-in and off by default), `service.py` (the one module
with side effects, mirroring `SchedulingService`/`OptimizationService`
exactly, plus the probe's own gating logic). `NetworkingV1Api` added to
`ClusterClients` (new, additive); `BatchV1Api` (already present,
read-only since Milestone 2) gets its first write use.

| Spec item | Status | Evidence |
|---|---|---|
| Service selectors and matching Pods | Implemented | `ServiceMatchResult.matched_pod_names`, exact label-equality, computed fresh from live Pod labels rather than read off the EndpointSlice |
| Pod readiness and EndpointSlice membership | Implemented | `ready_pod_names` (independent) compared against `endpointslice_ready_count` (the real controller's own output) -- a disagreement is itself a distinct finding, not noise |
| Service ports / target ports, named-port mismatches | Implemented | `_resolve_target_port()`: a numeric `targetPort` always routes (declaration is informational only); a named `targetPort` must resolve to a real declared `containerPort.name` or the Service port has no usable endpoint on that Pod -- the two cases carry different confidence, not collapsed into one bool |
| DNS configuration and CoreDNS health | Implemented | `check_dns_health()`: the `coredns` Deployment's real ready/desired replica counts and the `kube-dns` Service's real EndpointSlice ready-endpoint count, both read live |
| NetworkPolicies and relevant selectors | Implemented, static only | `build_applicable_policies()`: real `podSelector` evaluation (empty selector matches *every* Pod in the namespace -- the opposite of a Service's empty selector, implemented explicitly, not left to a fallthrough) and ingress/egress rule-presence summary. See the kindnet caveat below -- never a stronger claim than `POLICY_MAY_RESTRICT` |
| Namespace boundaries | Implemented | every lookup (`list_namespaced_service`/`_pod`/`_network_policy`/EndpointSlice) is scoped to the target Pod's own namespace; the one exception (`kube-system` for coredns/kube-dns) is RBAC-scoped to exactly those two named resources, not a general grant |
| Service types / routing | **Implemented** (closed as later, separate follow-up work; was "Partial" at the original audit) | `NodePort`/`LoadBalancer` need no separate routing logic -- both are strict supersets of `ClusterIP` (same selector/EndpointSlice/port-resolution checks already cover them), so `service_inventory.py` now additionally reads `Service.spec.type`, `ServicePort.nodePort`, and (for `LoadBalancer`) `status.loadBalancer.ingress` onto the existing `ServiceMatchResult`/`PortMatchResult` models -- additive fields, not a parallel analysis path. A pending `LoadBalancer` external address is surfaced in `NetworkFinding.reasoning` as informational evidence only, never a `classification` change: in-cluster routing to the target Pod is unaffected by whether its external address has been assigned yet. `ExternalName` needed no handling at all -- it has no selector/backend Pods, so it already, correctly, never appears as a selecting Service. See `docs/NETWORK_DIAGNOSTICS.md` §13. |
| Ingress / Gateway | **Not applicable** | confirmed live (`kubectl get ingressclass`, `kubectl get crd`) that zero such resources or CRDs exist anywhere in this project's cluster or manifest history -- an honest "not applicable," not a built-but-silent gap |
| Relevant Events | Detected via reuse | `resolve_pod_owner` (workload resolution) is reused as-is; Event correlation itself is the troubleshooting engine's own existing surface (`observer`/`diagnostics`), not duplicated here |
| Evidence-based request-path explanation | Implemented | `NetworkFinding.reasoning` always states the specific evidence behind the classification, and always appends the "not a confirmed end-to-end connectivity test" caveat -- API inspection cannot prove a packet actually arrives |
| Active, bounded diagnostic mechanism | Implemented, opt-in | `network/probe.py` + `NetworkService.run_probe` (docs/NETWORK_DIAGNOSTICS.md Section 12) -- a short-lived, auto-cleaned-up `batch/v1` Job tests real DNS resolution and a real TCP connect against a target Service's ClusterIP. This project's first mutation capability outside the fully policy-gated `RemediationExecutor`, closed only after its own dedicated design/safety review (Section 12): off by default (`network_probe_enabled=False`), its own namespace allowlist, cooldown, and concurrency limit, a fixed never-caller-built shell command, and a Pod template that passes every one of this project's own container-security checks by construction |
| Never claim a NetworkPolicy blocks traffic without evidence | Honored | `POLICY_MAY_RESTRICT` is the strongest classification an applicable policy can produce; the reasoning text never states a stronger claim, and the kindnet non-enforcement caveat is documented explicitly (below) |

**Environment constraint, confirmed still true and load-bearing here**:
this project's kind cluster uses kind's default CNI (kindnet), confirmed
via `lab/kind-config.yaml` (no `networking.disableDefaultCNI` override).
**kindnet does not enforce NetworkPolicy at all.** Any NetworkPolicy
finding this engine produces is therefore *static* analysis of the policy
object's selectors/ports/direction, never a claim about actually-observed
blocked traffic -- `POLICY_MAY_RESTRICT`, never "X is actually blocked." A
CNI swap (e.g. Calico) is the only way to close that gap, and is out of
scope unless explicitly requested.

**A real correction caught live during this milestone's own
verification**: the new `scenario-service-port-mismatch` lab scenario's
design doc originally assumed a named `targetPort` that can't resolve
would leave the EndpointSlice with zero ready endpoints. Confirmed live
against the real cluster that this is wrong -- the EndpointSlice
controller still adds a ready address for the matching Pod, just with
`ports: null` (no port it could resolve), so `endpointslice_consistent`
stays `true`. The scenario still correctly reaches `CONFIGURATION_ERROR`,
but via the independent port-match check alone, not via the
EndpointSlice-consistency check as originally assumed -- corrected in
`service.yaml`'s own comment and `FAILURE_LAB.md` rather than left
documented-wrong.

**Live-verified against the real cluster**: the existing
`service-selector-mismatch-demo` Service (selects no Pod) correctly
classifies as `INCONCLUSIVE` when checked against an unrelated Pod it
doesn't select, and a new `port-mismatch-demo` scenario (added this
milestone, see FAILURE_LAB.md) correctly classifies as
`CONFIGURATION_ERROR` from the named-targetPort mismatch alone, while its
owning Deployment is correctly resolved. DNS health reads the real
`coredns` Deployment (2/2 ready) and `kube-dns` Service (2 ready
endpoints) live, through the new `kube-system`-scoped RBAC grant --
confirmed via `kubectl auth can-i ... --as=system:serviceaccount:...` using
the real `TYPE/NAME` resourceNames syntax (plain `can-i` without a
resource name reports "no" for every one of these rules, since they're all
`resourceNames`-scoped -- the same lesson Capability B's `metrics.k8s.io`
case already taught, applied here from the start rather than re-discovered
the hard way). A live integration test
(`tests/integration/test_network_live.py`) patches the HTTP transport
itself and confirms a full `analyze_pod()` pass issues only `GET`.

**Live-verified again for the Service-type closure above**: applied a
real `LoadBalancer`-type Service and a real `NodePort`-type Service,
both selecting a real Deployment, to the actual `kind-kubesentinel`
cluster (no MetalLB or any other cloud-LB controller installed).
Confirmed via `kubectl get svc` and `kubesentinel network-check`
together: the `LoadBalancer` Service's `EXTERNAL-IP` genuinely stayed
`<pending>`, matched exactly by the engine's own `load_balancer_ingress: []`
plus its informational reasoning sentence, while `classification`
correctly stayed `HEALTHY` throughout. The `NodePort` Service's real,
API-server-allocated `nodePort` was read back via `PortMatchResult.node_port`,
matching `kubectl get svc`'s own output exactly.

## Capability E: Capacity Planning & Autoscaling Advisor

**Implemented** (Milestone 5 of `docs/PLATFORM_ROADMAP.md`;
`docs/CAPACITY_PLANNING.md` is the design). Additive work inside
`optimization/` -- a new `capacity.py` submodule, not a new top-level
package -- reusing `metrics_providers.py`/`InMemorySampleStore`/
`inventory.py`'s `WorkloadSnapshot`/`safety.py::apply_safety_checks`/
`OptimizationService.analyze()` end to end, exactly as the spec's own
"extend, don't duplicate the metrics collector" instruction asked.
`AutoscalingV2Api` added to `ClusterClients` (new, additive).

| Spec item | Status | Evidence |
|---|---|---|
| HPA status/targets | Implemented | `build_hpa_inventory()`/`_hpa_to_snapshot()`: real current/desired replicas, min/max bounds, every configured metric's current value vs. target, and the real `ScalingLimited` condition read verbatim -- matched to a `WorkloadSnapshot` by scale-target kind+name (an HPA has no UID reference to its target) |
| Missing requests make autoscaling math meaningless | Implemented | `_missing_requests_for_autoscaling()`: an HPA `Resource`/`Utilization` metric with no corresponding container request is flagged `MISSING_REQUESTS_FOR_AUTOSCALING_MATH`, deterministically from the HPA spec and the container's own requests |
| Additional replicas that would themselves be unschedulable | Implemented, coarse by design | `compute_scale_headroom()`: a cluster-wide, single-resource-at-a-time, greedy first-fit against real Node allocatable-minus-requested totals (reused from `scheduling/node_inventory.py`) -- only run for the specific "HPA at max and still over target" condition, never a full per-constraint simulation (that's `POST /scheduling/simulate`'s own job) |
| Basic usage trend | Implemented, explicitly labeled | `compute_trend()`: a two-half-mean comparison over the already-pooled sample window -- every finding's own reasoning states this is "a trend observed over the sampled window, not a forecast" |
| Memory leaks | Implemented as a pattern match, never a diagnosis | memory trend `INCREASING` over a long-enough window while CPU is not also increasing -> `POSSIBLE_MEMORY_LEAK_PATTERN`, reasoning explicitly states a leak is one explanation, not the only one, and that more replicas would delay, not fix, a per-replica leak |
| Single-threaded bottlenecks | Implemented as a narrow pattern match | `detect_bottleneck_pattern()`: at least `capacity_min_replicas_for_bottleneck_check` replicas independently plateauing in a band around one CPU core, with a configured limit allowing meaningfully more -> `POSSIBLE_SINGLE_THREADED_BOTTLENECK`. Computed from this call's own fresh per-Pod samples, not the cross-replica pooled bag `analyzer.py` uses for CPU/MEMORY sizing (pooling would hide exactly the per-replica signal this check needs) |
| External dependency as the real bottleneck | **Not detectable**, stated honestly | this project collects container CPU/memory usage only -- no request latency, no tracing, no dependency graph. Every finding says so explicitly when neither pattern above fires but an HPA still isn't meeting target, rather than silently assuming the cause is internal |
| "Prediction" labeled as extrapolation, not ML/forecasting | Honored | `TrendDirection` is a plain two-half-mean comparison, no statistical model of any kind; every finding's reasoning says so in the same sentence |
| No apply/write path | Honored | no code anywhere in this project patches an HPA's `minReplicas`/`maxReplicas` or a workload's replica count -- the same "advisory only" shape as every other optimization-engine category |

**A design decision worth naming**: capacity findings are produced at most
one per workload (never one per container, unlike `CPU`/`MEMORY`) --
replica count is a workload-level/Pod-level concern, using
`workload.containers[0]` as the representative container for the row,
the same precedent `analyzer.py::_scheduling_recommendation` already set
for its own workload-level finding.

**Zero new persistence, API routes, or CLI commands were needed** to ship
this capability -- confirmed by reading `persistence/optimization_repo.py`
(a `WorkloadSnapshot`/`ResourceRecommendation` is JSON-blob-serialized as
a whole; a new optional field and a new enum value needed no schema
migration) and `api/optimization_routes.py` (`category` is already a
typed `RecommendationCategory` Enum query param, so `?category=CAPACITY`
works, 422-on-invalid included, the moment the enum gains the value; and
`GET /optimization/workloads` already returns full `WorkloadSnapshot`
dicts, so the new `hpa` field appears there automatically). The CLI's
`--category` help string was the one line actually touched.

**Live-verified against the real cluster**: a new
`scenario-hpa-capacity-pressure` lab scenario (a real
`HorizontalPodAutoscaler` pinned at `minReplicas=maxReplicas=1` with a
deliberately tiny 1% CPU-utilization target, paired with a CPU-bound
busy-loop container) correctly produced a `CAPACITY` finding with
`HPA_AT_MAX` live, reading the real HPA's current utilization (~54-58%)
against its real 1% target and its real `ScalingLimited`/`TooManyReplicas`
condition. In this project's own lab environment as built (this
scenario's own ~1700m CPU request against the lab's other steady-state
scenarios' combined usage leaving the single kind Node at ~96% allocated),
`SCALING_WOULD_BE_UNSCHEDULABLE` also fired, confirmed via the real
Node's own allocatable-vs-requested numbers (3860m/4000m requested at
analysis time) -- this specific sub-finding is honestly documented as
environment-dependent (see `docs/CAPACITY_PLANNING.md` §11 and the
scenario manifests' own comments), not claimed as deterministic in every
environment. Confirmed via `kubesentinel optimize`/`kubesentinel
recommendations --category CAPACITY`/`kubesentinel recommendation <id>`,
a real HTTP server's `POST /optimization/analyze` and
`GET /optimization/recommendations?category=CAPACITY`, and
`GET /optimization/workloads` exposing the live `HpaSnapshot` with no
route changes. A live integration test
(`tests/integration/test_capacity_live.py`) patches the HTTP transport
itself and confirms a full `analyze()` pass issues only `GET`.

**A build-time correction, caught before shipping, not after**: the first
version of the lab scenario used an idle (`sleep infinity`) container,
reasoned to trivially exceed a 1% CPU target. Live-checking showed this
was wrong -- metrics-server rounds real usage below roughly 1m CPU down
to 0%, so an idle container reported `cpu: 0%/1%`, never triggering
`HPA_AT_MAX`'s "still over target" condition. Fixed by giving the
container a real (if synthetic) CPU-bound workload instead -- the kind of
gap this project's own "verify live, don't assume" discipline is meant to
catch.

## Capability F: Kubernetes Security & Configuration Auditor

**Implemented** (Milestone 6 of `docs/PLATFORM_ROADMAP.md`;
`docs/SECURITY_AUDITING.md` is the design). A new `security/` package:
`inventory.py` (one-shot read, Pods grouped by owning workload via the
reused `optimization/ownership.py::resolve_pod_owner`, RBAC objects,
NetworkPolicies, Deployments/StatefulSets/PodDisruptionBudgets,
Namespace labels), 15 rules across
`rules/{container,rbac,network_policy,availability,pod_security}.py` (a
14th, `EffectiveRbacEscalationRule`, closes `docs/SECURITY_AUDITING.md`
Section 12's own named "effective permissions across combined bindings"
gap -- see Section 13; a 15th, `NamespaceWithoutPodSecurityStandardRule`,
closes this same table's own "Pod Security Admission namespace labels"
row, below),
`engine.py` (mirrors `diagnostics/engine.py::DiagnosticEngine` exactly),
`service.py` (the one module with side effects). `RbacAuthorizationV1Api`
and `PolicyV1Api` added to `ClusterClients` (new, additive).

| Spec item | Status | Evidence |
|---|---|---|
| Container privilege/security-context settings | Implemented | 8 rules: `PRIVILEGED_CONTAINER` (CRITICAL), `ALLOW_PRIVILEGE_ESCALATION` (HIGH if true, LOW if merely unset -- absence is itself evidence, same discipline `_data_quality_recommendation` already established), `RUN_AS_ROOT` (effective value considering Pod-level fallback), `MISSING_READ_ONLY_ROOT_FS`, `HOST_NAMESPACE_SHARING`, `HOST_PATH_VOLUME`, `MUTABLE_IMAGE_TAG` (reusing the exact gotcha already documented in TROUBLESHOOTING.md for an unrelated reason), `SECRET_AS_ENV_VAR` |
| RBAC over-permissiveness | Implemented | `CLUSTER_ADMIN_BINDING` (CRITICAL cluster-wide / HIGH namespace-scoped) and `WILDCARD_RBAC_RULE` (HIGH) -- both excluding Kubernetes' own built-in, unfixable Roles/bindings by name-prefix/exact-name, confirmed live to need *two* built-in Group prefixes (`system:`, `kubeadm:`), not the one originally assumed |
| Missing/overly-broad NetworkPolicies | Implemented, reused not re-read | `NAMESPACE_WITHOUT_NETWORK_POLICY` and `UNRESTRICTED_INGRESS_POLICY`, the latter reusing `network/policy_inventory.py::has_unrestricted_ingress_rule`/`summarize_policy` (both promoted to public for this reuse) rather than re-reading and re-classifying NetworkPolicy objects a second time, per the roadmap's own explicit instruction |
| PodDisruptionBudget coverage | Implemented | `MISSING_PDB_FOR_MULTI_REPLICA_WORKLOAD`, the roadmap's own named optional item, included since `PolicyV1Api` was simple to add. Uses standard Kubernetes LabelSelector semantics (null selector matches nothing, empty-but-present matches everything) -- deliberately a *separate*, correctly-scoped helper from `network/policy_inventory.py`'s own podSelector matching, which uses the opposite null-handling for a documented, NetworkPolicy-specific reason |
| Finding shape: ID, rule ID, severity, evidence, explanation, remediation | Implemented | `SecurityFinding`, individually persisted and addressable (`get_finding`/`list_findings`), the same first-class-entity shape `SchedulingFinding`/`NetworkFinding` already established |
| Applicable/inconclusive distinction | Implemented at the run level | `SecurityAuditResult.warnings`: a rule family whose own inventory read failed (e.g. 403 listing ClusterRoleBindings) is named there, distinct from "evaluated and found nothing" |
| Build vs. integrate an external scanner | Decided: build the *core* catalog natively | no kube-linter/Kubescape integration for container-security/RBAC/NetworkPolicy/PDB -- consistent with every other capability being a native, in-repo rule engine; see docs/SECURITY_AUDITING.md Section 2. CVE scanning specifically is the one named exception, below. |
| CVE/image vulnerability scanning | Implemented, opt-in | `IMAGE_HAS_KNOWN_VULNERABILITIES` via a local Trivy binary (`security/image_scanning.py`, docs/SECURITY_AUDITING.md Section 14) -- off by default (`security_enable_image_scanning=False`); a missing `trivy` binary or a failed scan degrades to a `warnings` entry, never aborts the audit. The only module in this engine that isn't a pure `SecurityRule` (it shells out) and the only one with a new, non-Kubernetes-RBAC dependency (outbound network access for Trivy's own vulnerability DB and image pulls) |
| Pod Security Admission namespace labels | **Implemented** (closed as later, separate follow-up work; was named "deliberately not built... redundant" at the original audit -- that framing undersold it) | `NamespaceWithoutPodSecurityStandardRule` (`security/rules/pod_security.py`, `NAMESPACE_WITHOUT_POD_SECURITY_STANDARD`, LOW): a namespace with running Pods and none of `pod-security.kubernetes.io/{enforce,audit,warn}` set. Not actually redundant with the container-level checks, on reflection -- those are *detective* (confirm a problem after a Pod already exists); this is *preventive* (would reject an unsafe Pod spec at admission, before creation). `SecurityInventory.namespace_labels` (new cluster-scoped `Namespace` LIST, `rbac/security-clusterrole.yaml` grown with a `namespaces` `get`/`list` rule rather than a new ClusterRole -- Namespace is cluster-scoped for the same reason ClusterRole/ClusterRoleBinding already are in that same file) feeds it; same `optimization_excluded_namespace_list` exclusion `NamespaceWithoutNetworkPolicyRule` already reuses. See `docs/SECURITY_AUDITING.md` §15. |

**Two real findings caught live during this milestone's own
verification, by this project's own Milestone 20-23 audit standard**:

1. The RBAC rule's built-in-subject exclusion first assumed only
   `system:masters` needed excluding. Live-checking this project's own
   lab cluster's real `ClusterRoleBindings` (`kubectl get
   clusterrolebindings`) showed a *second* genuine built-in binding --
   `kubeadm:cluster-admins`, bound to the Group kubeadm itself uses for
   `admin.conf` -- being mis-flagged as a real finding. Fixed by
   excluding Group subjects by *prefix* (`system:`, `kubeadm:`,
   configurable via `security_builtin_subject_group_prefixes`) rather
   than one hardcoded exact name.
2. `SecurityService.audit()` re-evaluates the entire cluster on every
   call, but the first persistence design (an upsert-by-`finding_id`,
   copied from `NetworkFinding`/`SchedulingFinding`'s own per-Pod shape)
   silently accumulated every past run's findings forever -- caught live
   when the fix for gap #1 was applied and the *stale*, now-incorrect
   `kubeadm:cluster-admins` finding from the previous run was still
   showing up in `kubesentinel security-findings` alongside the corrected
   result. Fixed by replacing the per-finding upsert with a whole-run
   replace (`SecurityRepository.save_findings`), the same semantics
   `optimization_repo.py::save_workload_snapshots` already uses for its
   own full-cluster-rescan shape -- a real, found-live mismatch between
   "one finding per Pod" and "a full cluster re-scan every call," not an
   oversight that would have been caught by unit tests alone (the unit
   tests were written against the already-correct `save_findings`
   contract; this gap was only visible by actually running two live
   audits in sequence).

**Live-verified against the real cluster**: a new
`scenario-security-findings` lab scenario (a privileged container, an
over-permissive `ClusterRoleBinding` on a throwaway ServiceAccount, and a
NetworkPolicy with an unrestricted ingress rule) correctly produced
`PRIVILEGED_CONTAINER`, `CLUSTER_ADMIN_BINDING`, and
`UNRESTRICTED_INGRESS_POLICY` findings live, while the cluster's own two
real built-in RBAC bootstrap bindings (`system:masters`,
`kubeadm:cluster-admins`) were correctly never flagged. The existing
`remediation-test` Deployment (2 replicas, no PodDisruptionBudget) and
the lab namespace's own pre-existing lack of any NetworkPolicy both
triggered their respective coverage findings without any new manifest
needed. Confirmed via `kubesentinel security-audit`/`security-findings
--severity CRITICAL`/`security-finding <id>`, a real HTTP server's `POST
/security/audit` and `GET /security/findings?severity=CRITICAL` (422 on
an invalid severity confirmed too), and that a second `audit()` call
leaves the repository showing only the current run's findings, not the
first run's stale ones. A live integration test
(`tests/integration/test_security_live.py`) patches the HTTP transport
itself and confirms a full `audit()` pass issues only `GET`.

## Shared policy, approval, and remediation layer

**Implemented for Capability A from the start; now also for Capability B's
own apply path (§14 of its design doc) -- this section's own forward-looking
prediction, below, closed exactly as it anticipated, not bypassed.**

`policy/engine.py::PolicyEngine` gates every troubleshooting-engine
mutation (`propose()` -> `evaluate()` before anything is built or
persisted) with mode (OBSERVE/APPROVAL_REQUIRED/CONTROLLED_AUTO),
namespace/resource-name allowlists, cooldowns, and per-incident/per-window
budgets -- see REMEDIATION_SAFETY.md for the full, live-verified list.

The resource optimization engine's `review()` (approve/reject/mark-reviewed
a recommendation) still does **not** go through `PolicyEngine` -- it has
no `ActionType`, no mode check, no cooldown, no budget, by the same
deliberate Milestone 16 design choice as always (a review is a decision
*record*, never a cluster mutation). `apply_recommendation()`
(`RESOURCE_OPTIMIZATION_DESIGN.md` §14) is a different call, and does go
through `PolicyEngine` explicitly, exactly as this section originally
predicted it would have to: a new `ActionType.ADJUST_RESOURCES`, gated by
the same mode/allowlist/cooldown/budget checks every other action type
already gets, generalized (`incident_id`/`incident_severity` now
`Optional`) rather than duplicated so a standalone action -- one with no
owning Incident, the common case for a recommendation -- fits the exact
same lifecycle instead of a parallel one.

The network engine's active connectivity probe
(`NETWORK_DIAGNOSTICS.md` §12) is the one new mutation surface that
*deliberately does not* route through `PolicyEngine` -- not an oversight,
a reasoned exception named in its own design doc: `PolicyEngine`/
`ActionType`/`RemediationAction` model "mutate an existing Deployment,"
and don't fit "create and delete a throwaway diagnostic Job" at all. It
gets its own small, dedicated, equally-rigorous gate instead (its own
enable flag, namespace allowlist, cooldown, and concurrency limit).
Nothing about `PolicyEngine`'s current shape auto-applies to a new
`ActionType` without each new executor method explicitly calling into
it, the same way `RemediationExecutor` does today -- Capability B's own
`_adjust_resources` method is the second proof of that, not the first.

Already satisfied for *any* future mutation by construction, not by
policy alone: no cluster-admin RBAC anywhere (`rbac/*.yaml` is entirely
least-privilege, additive, and 100% read-only outside
`remediation-role.yaml` and `network-probe-role.yaml`); AI output has no
path back to any decision (confirmed above). The "no arbitrary shell/
command execution anywhere in this codebase" claim this line used to make
no longer holds unconditionally -- `security/image_scanning.py` (Trivy,
opt-in, off by default) is a real, new `subprocess` call, named
explicitly in its own design doc's own safety section rather than
silently breaking this claim; every *other* code path in this project
still has zero `subprocess`/`os.system` calls outside test fixtures and
the one `kubectl create token` test helper. (`network/probe.py`'s own
mutation is a Kubernetes API call, same mechanism as every other
executor method in this project, not a new shell-out.)

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

**Implemented** (Milestone 8 of `docs/PLATFORM_ROADMAP.md`). One CRD,
`KubeSentinelRemediationPolicy` (`crds/kubesentinelremediationpolicy.yaml`,
namespaced, group `kubesentinel.io`), reconciled by a thin controller
(a new `operator/` package: `operator/controller.py::RemediationPolicyController`,
run via the new `kubesentinel operator-run` CLI command, blocking like
the API server does). No `kopf`/`kubebuilder` dependency: the watch is a seventh,
thin `CustomResourceWatcher` alongside the six existing watchers in
`observer/watchers.py`, reusing the exact same proven list-then-watch
loop -- a deliberate decision, not an oversight, since one already
existed in this codebase that already handles dict-returning resources
(`EndpointSliceWatcher`'s own precedent). See `docs/OPERATOR_DESIGN.md`
for the full design, in particular §2's load-bearing constraint: the
controller never decides which `ActionType` to propose for an incident
(that remains entirely human/CLI/API-driven via `plan`, exactly as
before) -- it only re-evaluates an already-proposed, still-
`PENDING_APPROVAL` action against the policy the CR currently declares,
calling the *exact* same `PolicyEngine.evaluate()`/
`RemediationService.approve()`/`.execute()` every other caller already
uses. "Any actual remediation triggered through the operator still goes
through the same approval mechanism everything else does" (the spec's
own words) is satisfied literally: the controller calls `approve()`, it
does not skip it.

Live-verified against the real cluster, catching one real bug along the
way: the controller's own re-evaluation of an already-persisted pending
action initially included that very action in its own `recent_actions`
cooldown check, denying it against a 0-second-old "cooldown" triggered by
itself -- `propose()` never hits this (it builds `recent_actions` before
the new action is ever saved), but re-evaluating an action *after* it's
already persisted needed an explicit self-exclusion. Fixed
(`operator/controller.py::_maybe_auto_approve`), caught by this
milestone's own unit tests before it ever reached the live cluster. Live
run against a real `crashloop-demo` incident confirmed: under
`CONTROLLED_AUTO`, a pending action whose diagnosing rule forbids
auto-remediation (true of every rule in this codebase today, by design)
is correctly left `PENDING_APPROVAL`; after manually marking a copy
auto-remediation-eligible and temporarily scaling the target to 2
replicas (restored afterward) to clear the single-replica safety check,
the operator correctly auto-approved and executed a real Deployment
restart (`kubectl.kubernetes.io/restartedAt` annotation confirmed
updated on the real Deployment), with `approved_by: kubesentinel-operator`
recorded in the incident's own audit trail and an honest
`VERIFICATION_INCONCLUSIVE` outcome (a restart never fixes this
scenario's deterministic crash -- correctly not reported as success).

## Controlled Chaos & Resilience Lab

**Implemented** (Milestone 7 of `docs/PLATFORM_ROADMAP.md`). The
*scenario* half was already strong and live-verified: 16 lab scenario
directories under `lab/manifests/`, each with setup instructions, expected
symptoms, expected diagnosis, and cleanup -- documented per-scenario in
FAILURE_LAB.md. `lab/scripts/run-scenario.sh` and `lab/scripts/cleanup.sh`
remain the generic, scenario-agnostic appliers, and every scenario stays
scoped to the single `kubesentinel-lab` namespace.

What was missing -- the **evaluation harness** the spec asks for -- is now
built: `evaluation/harness.py::EvaluationHarness` deletes-then-reapplies a
scenario fresh (a genuine, verified-absent baseline, not whatever stale
state the shared lab happened to be in), then measures time-to-detection/
time-to-diagnosis via a harness-owned `ObserverService` for the push-based
troubleshooting engine, or by re-triggering `analyze()`/`audit()` each poll
for the five pull-based engines (optimization, scheduling, network,
capacity, security). Recovery time is measured only for
`scenario-transient-recovery` (the one scenario that defines a recovery
path at all); false-positive rate is a whole-battery property, computed
from every currently-open incident/finding across all five repositories
that cannot be attributed to any registered scenario's own resource name.
Results persist via a sixth `EvaluationRepository`/`SQLiteEvaluationRun`
pair, behind `kubesentinel evaluate`/`evaluation-runs`/`evaluation-run`
(CLI-only -- deliberately never an HTTP `POST`, since running a battery
mutates the shared lab namespace; `GET /evaluation/runs[/{id}]` serves
already-persisted runs). See `docs/CHAOS_RESILIENCE_LAB.md` for the full
design, including why time-to-detection and time-to-diagnosis are the same
number for every TROUBLESHOOTING-capability scenario in this architecture.

Live-verified against the real cluster across all 5 capabilities
(`failed-mount`, `missing-requests`, `pending-resources-scheduling-fit`,
`security-findings`, `service-port-mismatch`) -- all 5 reported `DETECTED`
with the correct observed value. This run caught a real bug: a scenario
can open *more than one* real incident (`scenario-failed-mount` opens both
a Deployment-level `UNSATISFIED_REPLICAS` incident and the real Pod-level
`FAILED_MOUNT` one, since both resource names start with the same
`resource_name_prefix`), and the harness's first version took whichever
one the repository listed first rather than the one whose own diagnosis
matched -- reporting a false `WRONG_CATEGORY`. Fixed to keep polling for
an exact match until the deadline, confirmed live afterward.

## Observability

**Implemented for every capability** (corrected here from "A, B, and C"
at the original audit -- D, E, and F each already followed the exact
same convention by the time they shipped, this line just hadn't been
updated to say so), following one consistent, bounded-cardinality
labeling discipline throughout (`category`, `action_type`, `reason`,
`component`, `status`, `provider`, `result` -- never an incident ID,
recommendation ID, finding ID, or Pod name as a label; confirmed by
reading every `Counter`/`Gauge`/`Histogram` definition in
`metrics/registry.py`, including `scheduling_analysis_total`,
`network_analysis_total`/`network_probe_total`,
`security_audit_total`/`security_findings_total`). `observability/prometheus.yml`
+ `grafana-dashboard.json` exist and are verified against real
`/metrics` output.

**Closed as later, separate follow-up work -- was left as an explicit,
undecided question at the original audit**: "No per-module 'degraded /
unknown / not applicable' distinction exists as a first-class concept
yet... worth deciding explicitly before C/D/E/F each invent their own
version of the same idea." They did, exactly as predicted --
`AnalysisRunSummary.metrics_available`+`.warning` (Capability B),
`SecurityAuditResult.warnings` with no completeness verdict alongside
it (Capability F), and `SchedulingFinding`/`NetworkFinding` with no
equivalent at all (C/D). The decision: a shared, typed
`EvidenceCompleteness` enum (`COMPLETE`/`DEGRADED`/`UNKNOWN`/
`NOT_APPLICABLE`) via a common `EvidenceCompletenessMixin` base model,
included by `AnalysisRunSummary`, `SecurityAuditResult`, and
`SchedulingFinding` -- additive, never a rename: every pre-existing
field (`warning`, `metrics_available`, `SecurityAuditResult`'s own
already-identically-named `warnings`) is kept exactly as it was.
Closing a real gap found while wiring this up, not just a cosmetic
addition: `SchedulingFinding` had no way to surface a sub-read failure
at all -- `analyze_hypothetical`'s own ResourceQuota/LimitRange reads
already degraded on failure but only logged it, never told the caller,
so a finding could silently report "no ResourceQuota in this
namespace" when the true state was "couldn't check." `NetworkFinding`
is deliberately *not* included in this round: `network/service.py::analyze_pod`
is genuinely all-or-nothing (no sub-read there is individually
try/except-wrapped the way optimization's/security's/scheduling's own
inventory builders already are), so there is no partial-degrade state
to attach a `warnings` field to without first changing that
engine's own error-handling granularity -- named explicitly as a real,
structural difference, not silently assumed covered. See
`docs/EVIDENCE_COMPLETENESS.md` for the full design.

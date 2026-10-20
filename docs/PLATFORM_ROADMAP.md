# Platform roadmap

Milestone numbering matches the platform specification's own section 15
numbering (Milestone 0 through 9), so this document stays easy to cross-
reference against the spec that drove it. This is a forward-looking plan,
not a build log -- TODO.md remains the authoritative, append-only record of
what was actually done and live-verified, for every milestone across this
entire project's history (the original 11-milestone build, the prior
8-milestone resource-optimization extension, and every milestone below
once it's actually completed). This document gets updated as scope shifts;
TODO.md never does.

See `docs/IMPLEMENTATION_STATUS.md` for the evidence-backed detail behind
every status claim below, and `docs/MASTER_ARCHITECTURE.md` for the shared
architectural pattern every milestone from here on should follow.

## Milestone 0: Audit and architecture -- DONE

This milestone. Inspected the repository, ran the baseline test suite
(410 unit tests passing, ruff/mypy clean; `--all` not independently
re-verified clean today due to this machine's documented shared-VM
contention, but already verified passing -- 442 total -- multiple times in
the immediately preceding session), and produced `IMPLEMENTATION_STATUS.md`,
`MASTER_ARCHITECTURE.md`, and this document. No code changed.

## Milestone 1: Stabilize the existing core -- DONE

**Scope, adapted to what the audit actually found**: the spec frames this
as "ensure the observer, troubleshooting engine, incident model, audit
layer, and safe remediation workflow work reliably" -- the audit found all
of that already true, tested, and live-verified (410 unit tests, 32
integration/e2e tests covering exactly this, RBAC boundary enforcement,
concurrent-remediation correctness, recovery verification). There was no
large stabilization backlog to work through here; inventing one would have
contradicted the spec's own instruction not to rewrite working components
without a concrete reason.

The audit's three named, optional gaps were picked up after Milestones
2-6 shipped (per the user's own explicit request to "tackle those 3" before
continuing to Milestone 7) -- all three closed, full test suite green
throughout, no change to any existing passing test's assertion:

1. **`UsageSummary.throttled` is scaffolded but never populated** --
   **implemented**, not removed: `analyzer.py::_check_throttled()` runs
   the real `container_cpu_cfs_throttled_seconds_total` Prometheus query.
   A confirmed result states the fact explicitly and vetoes a proposed
   CPU decrease (the same "real pressure vetoes a decrease" rule already
   applied to a recorded OOM kill); any query failure or metrics-server-
   only evidence leaves it `None`, never coerced to `False`. Unit-tested
   against a mocked Prometheus response -- Prometheus itself has never
   been deployed in this project's own lab, so this stays at the same
   verification tier as every other Prometheus-dependent path here.
2. **No in-Pod mount-failure diagnostic rule** -- **implemented**:
   `diagnostics/rules/mount.py::FailedMountRule`, reading the kubelet's
   own `FailedMount` Warning Event, plus adding `"FailedMount"` to
   `incidents/health.py::_BAD_EVENT_REASONS` (without which a Pod stuck
   this way would never open an incident at all). Live-verified via a new
   `scenario-failed-mount` lab scenario -- honestly, a missing-ConfigMap
   volume reference, not the PVC-with-unreachable-storage case that
   motivated the rule, since this lab's own kind-on-local-path setup has
   no real external storage infrastructure to break deterministically;
   confirmed live before writing the manifest that this still produces a
   real `FailedMount` Event, which the rule itself is agnostic to the
   cause of. This live pass through the *real* `ObserverService` (not
   just the one-shot `diagnose` CLI, which never fetches Events at all --
   a separate, pre-existing, structural limitation, confirmed and left
   alone) caught a genuine second bug: `observer/service.py`'s own
   `_RELEVANT_EVENT_REASONS` set -- a second, independent Event-reason
   filter gating the `EventWatcher` itself, upstream of and separate from
   `incidents/health.py::_BAD_EVENT_REASONS` -- also needed `"FailedMount"`
   added, or the real watcher silently dropped the Event before
   `IncidentManager.observe()` ever saw it. Fixed, regression-tested
   (`test_observer_service.py::test_failed_mount_event_is_forwarded`), and
   confirmed live afterward: the real Pod incident now opens and is
   diagnosed `FAILED_MOUNT` (HIGH confidence) end to end.
3. **No cross-link from a resource-optimization finding back into an open
   incident's own hypothesis list** -- **implemented**, deliberately
   narrow for its first case: `Hypothesis.related_recommendation_ids`,
   populated by `IncidentManager._enrich_with_recommendations` only for
   `OOM_KILLED` (the roadmap's own named example) against open `MEMORY`/
   `DATA_QUALITY` recommendations for the same workload. `IncidentManager`
   gained an optional `optimization_repo` parameter (additive; every
   existing caller passing only `repo` is unaffected) -- the diagnostic
   engine itself stays a pure function of evidence and never queries a
   repository, matching the project's own "deterministic first" rule.
   Unit-tested against real `IncidentManager`/`Hypothesis`/`Incident`
   objects and a real `SQLiteOptimizationRepository` (6 tests). A full
   live demonstration against the lab's own `oom-demo` scenario was
   attempted but is blocked by two independent, pre-existing environmental
   facts, neither introduced by nor fixable within this change: (a) this
   kind cluster's container runtime reports plain `Error` rather than
   `OOMKilled` for this workload's kills, so `OOMKilledRule` only ever
   produces its MEDIUM-confidence *inferred* hypothesis here, which loses
   the per-incident ranking to the HIGH-confidence `CrashLoopBackOffRule`
   hypothesis on the same Pod -- a correct, already-documented behavior of
   `OOMKilledRule` itself (see its own docstring), unrelated to this
   cross-link; and (b) a real `kubesentinel optimize` pass against the lab
   produced zero recommendations for `oom-demo` at all, because a
   perpetually crash-looping Pod never holds a long enough live window for
   metrics-server to return a usage sample. Given both, the unit-test tier
   is the correct and sufficient verification here, for the same reason
   given for item 1 above: there is no live path to exercise that doesn't
   itself depend on fixing unrelated, already-correct, already-documented
   behavior elsewhere in the system.

## Milestone 2: Resource optimization -- DONE

Already fully built, in a prior extension to this same repository, before
this platform specification existed. See `RESOURCE_OPTIMIZATION_DESIGN.md`
for the design and TODO.md's Milestones 12-19 for the complete
build-and-verification history (typed models, quantity parsing, workload
inventory with correct owner-reference resolution, the Metrics API +
optional Prometheus provider interface, deterministic CPU/memory
right-sizing, safety checks, API/CLI/persistence, incident integration,
Prometheus metrics, RBAC, live kind-lab scenarios, and documentation).
`IMPLEMENTATION_STATUS.md`'s Capability B section is the authoritative,
audited status -- including the one real scaffolded-but-unpopulated gap
(CPU throttling) named under Milestone 1 above.

## Milestone 3: Scheduling advisor -- DONE

See `docs/SCHEDULING_ANALYSIS.md` for the design and TODO.md's own entry
for the full build-and-verification history. `docs/IMPLEMENTATION_STATUS.md`'s
Capability C section is the authoritative, audited status. Topology spread
constraints, pod affinity/anti-affinity, and PriorityClass/preemption
simulation were scoped out of this milestone, as planned below, and remain
`unevaluated_constraints` a finding reports rather than guesses at.

**Original scope** (for reference; superseded by what's actually built,
above): a new `scheduling/` package. Reuses
`diagnostics/rules/scheduling.py::classify_unschedulable_message()` as the
coarse first pass (already proven, already reused once by the optimization
engine), then adds the independent constraint reconstruction that message
classification alone cannot provide: real Node allocatable-vs-requested
math (needs a new, additive `core.list_node()` call inside this package --
no new `ClusterClients` field required, `core` already exists), real
taint/toleration matching, real node-affinity/selector evaluation, and
(where present) topology spread constraint and `PriorityClass`/preemption
evidence.

**Explicitly scoped out, per the spec's own instruction**: no replacement
scheduler. A read-only placement *simulation* ("would this Pod's current
spec be schedulable, and where") is in scope; actually scoring and ranking
nodes the way `kube-scheduler`'s own scoring plugins do is not, and any
simulation this milestone builds must document its own approximations
rather than imply equivalence with the real scheduler.

**Environment note already on record** (see `IMPLEMENTATION_STATUS.md`):
the lab's single-node kind cluster can demonstrate real allocatable math
and real taint/affinity evaluation, but not a genuine multi-node
trade-off. Whether to add a second, optional multi-node `kind-config`
variant specifically for this capability's own demo is a decision for the
start of this milestone, not pre-made here.

**This milestone's own first step**, matching every prior capability's own
established pattern (`RESOURCE_OPTIMIZATION_DESIGN.md` was written before
any scheduling-engine code): a `docs/SCHEDULING_ANALYSIS.md` design pass,
covering the exact data model, the exact constraint-evaluation algorithm,
and the exact API/CLI surface, before implementation begins.

## Milestone 4: Network troubleshooting -- DONE

See `docs/NETWORK_DIAGNOSTICS.md` for the design and TODO.md's own entry
for the full build-and-verification history. A new `network/` package
(`service_inventory.py`, `policy_inventory.py`, `dns_health.py`,
`analyzer.py`, `service.py`); `NetworkingV1Api` added to `ClusterClients`
(new, additive). Builds the independently-computed
`Pod -> DNS -> Service -> EndpointSlice -> destination Pod` evidence chain
the spec asks for: Service selector-vs-Pod-label matching (computed fresh,
not inferred from EndpointSlice counts), an EndpointSlice-vs-selector
consistency check, numeric-vs-named `targetPort` resolution with
Kubernetes' real asymmetric semantics, CoreDNS/`kube-dns` health, and
applicable NetworkPolicy summaries. Every finding's reasoning always
states the API-inspection-cannot-prove-packet-delivery caveat explicitly.

**Deliberately deferred, as a genuine scope decision made during this
milestone's own design pass, not an oversight**: the spec's "bounded,
explicitly enabled diagnostic mechanism" active DNS/connectivity probe.
Reasoned as this project's first mutation capability outside the fully
policy-gated `RemediationExecutor`, deserving its own dedicated
design/safety review rather than folding a new mutation surface into an
already-large static-analysis milestone. **Since built, as that later,
separate, dedicated-review milestone -- see `docs/NETWORK_DIAGNOSTICS.md`
§12** (`network/probe.py`, off by default, explicitly opted into per
call). `EvidenceKind.SERVICE_SPEC` remains reserved-but-unproduced.
`ReasonCode.DNS_RESOLUTION_FAILURE` does too, but no longer for the reason
given when this section was first written -- see
`docs/IMPLEMENTATION_STATUS.md`'s own "DNS and connectivity problems" row
for the now-precise reason (the probe's own shape -- slow, explicit,
rate-limited, mutating -- is structurally incompatible with
`DiagnosticEngine`'s pure, synchronous, always-on rules, not merely
something that hadn't been built yet). **Ingress/Gateway**: confirmed live
(`kubectl get ingressclass` / `kubectl get crd`) that zero such resources
or CRDs exist anywhere in this project's cluster or manifest history -- an
honest "not applicable," not a built-but-silent gap.

**Environment constraint already on record, and load-bearing here**:
kindnet (this cluster's default CNI) does not enforce NetworkPolicy. Every
NetworkPolicy-related finding this milestone produces is explicit that it
is static analysis of the policy object (`POLICY_MAY_RESTRICT`, never a
stronger claim), never a claim about actually-observed blocked traffic.

## Milestone 5: Capacity planning and autoscaling advisor -- DONE

See `docs/CAPACITY_PLANNING.md` for the design and TODO.md's own entry
for the full build-and-verification history. True to the spec's own
explicit instruction not to build a second metrics collector, this
shipped as additive work inside `optimization/` -- a new `capacity.py`
submodule, not a new top-level `capacity/` package -- reusing the
existing `WorkloadSnapshot`/`InMemorySampleStore`/`MetricsApiProvider`/
`apply_safety_checks`/`OptimizationService.analyze()` pipeline end to
end. `AutoscalingV2Api` added to `ClusterClients` (new, additive) to read
real `HorizontalPodAutoscaler` status/targets, matched to a
`WorkloadSnapshot` by scale-target kind+name. Real Node allocatable-vs-
requested totals are reused from `scheduling/node_inventory.py` (the
opposite import direction from Milestone 4) for the one new
cluster-headroom check.

Each of the spec's five "when more replicas help vs. when they don't"
cases was addressed honestly, not uniformly built: HPA status, missing
requests for Utilization-based autoscaling math, and a coarse
scale-headroom check are evaluated for real against live cluster state;
memory-leak and single-threaded-bottleneck signals are reported as
*pattern matches* over the existing sample window, explicitly labeled as
such rather than a diagnosis; an external dependency as the real
bottleneck is named as a hard limitation this project has no evidence
source for at all (no latency/tracing data), never silently assumed
covered. No apply/write path exists for any of this -- consistent with
the rest of the optimization engine.

**Zero new persistence, API routes, or CLI commands were needed** --
`RecommendationCategory.CAPACITY` and `WorkloadSnapshot.hpa` are both
purely additive fields/enum values on structures that are already
JSON-blob-persisted and already exposed generically by
`GET /optimization/recommendations?category=...` and
`GET /optimization/workloads`, confirmed by reading the persistence
layer before assuming a migration was needed.

## Milestone 6: Security and configuration auditor -- DONE

See `docs/SECURITY_AUDITING.md` for the design and TODO.md's own entry
for the full build-and-verification history. A new `security/` package:
`inventory.py` (one-shot read of Pods grouped by owning workload, RBAC
objects, NetworkPolicies, Deployments/StatefulSets/PodDisruptionBudgets),
`rules/{container,rbac,network_policy,availability}.py` (13 rules total),
`engine.py` (mirrors `diagnostics/engine.py::DiagnosticEngine` exactly),
`service.py`. `RbacAuthorizationV1Api` and `PolicyV1Api` added to
`ClusterClients` (new, additive).

**Build vs. integrate, decided**: a native rule catalog, not kube-linter/
Kubescape -- consistent with every other capability in this project being
a native, deterministic, in-repo rule engine with zero external-scanner
subprocess dependency; see docs/SECURITY_AUDITING.md Section 2 for the
full reasoning.

**Scope, evaluated for real**: container security context (privileged,
allowPrivilegeEscalation, runAsNonRoot, readOnlyRootFilesystem, host
namespace sharing, hostPath volumes, mutable image tags, Secrets as env
vars), RBAC over-permissiveness (cluster-admin bindings, wildcard
verb+resource/apiGroup rules -- both excluding Kubernetes' own built-in,
unfixable Roles/bindings), NetworkPolicy coverage (namespaces with zero
policies, policies with an unrestricted-allow-all ingress rule, reusing
Milestone 4's own classification rather than re-reading NetworkPolicy
objects a second time), and PodDisruptionBudget coverage for multi-replica
workloads. **Deliberately deferred at this milestone, both since closed
as later, separate follow-ups**: CVE/image vulnerability scanning (no
maintained vulnerability database this project had access to at the
time -- closed as an opt-in extra via a local Trivy binary, see
SECURITY_AUDITING.md §14) and Pod Security Admission namespace-label
auditing (originally thought redundant with the already-built,
more granular container-level checks -- closed once that framing was
found to undersell the gap: those checks are all detective, PSA is
preventive, see SECURITY_AUDITING.md §15). **Still deferred**:
ServiceAccount token automount auditing and any compliance-framework
scoring -- each named explicitly as a gap, not silently absent.

**A real finding caught live during this milestone's own verification,
by this project's own Milestone 20-23 audit standard**: a real kind
cluster has *two* built-in cluster-admin Group bindings, not the one
better-known `system:masters` this milestone first assumed --
`kubeadm:cluster-admins` (kubeadm's own, used by `admin.conf`) was
initially mis-flagged as a real finding until caught by reading this
project's own lab cluster's actual `ClusterRoleBindings` rather than
trusting the first assumption. A second, independent gap caught the same
way: `SecurityService.audit()` re-evaluates the entire cluster on every
call, but the first persistence design silently accumulated every past
run's findings forever (an upsert-by-`finding_id` design borrowed from
`NetworkFinding`/`SchedulingFinding`, which call `analyze_pod()` once per
*Pod* rather than re-scanning the whole cluster -- a real, found-live
mismatch between those two shapes). Fixed by replacing the per-finding
upsert with a whole-run replace (`save_findings`), the same semantics
`optimization_repo.py::save_workload_snapshots` already uses for its own
full-rescan shape.

## Milestone 7: Chaos and resilience lab -- DONE

Built the **evaluation harness** named as missing in
`IMPLEMENTATION_STATUS.md`, not new failure scenarios (all 16 already
existed and were already live-verified). See `docs/CHAOS_RESILIENCE_LAB.md`
for the full design. Covers every one of the six analysis engines, not
just the original troubleshooting scenarios: a scenario registry
(`evaluation/scenarios.py`) transcribes each manifest's own already-
reviewed "Expected diagnosis" comment as ground truth, dispatched per
scenario to whichever capability owns it.

Push (troubleshooting) vs. pull (optimization/scheduling/network/
capacity/security) detection genuinely need different measurement
strategies -- a harness-owned, short-lived `ObserverService` for the
former; re-triggering `analyze()`/`audit()` each poll for the latter five
-- documented honestly rather than forced into one shape, including that
time-to-detection and time-to-diagnosis are the same number for every
troubleshooting-capability scenario in this architecture (there is no
separate detect-then-diagnose phase). Recovery time is measured only for
the one scenario that defines a recovery path at all; false-positive rate
is a whole-battery property against real, currently-open findings, not a
synthetic injection.

Live-verified across all 5 capabilities (one representative scenario
each) -- all 5 correctly `DETECTED`. This caught a real bug: a scenario
can open more than one real incident sharing the same resource-name
prefix (`scenario-failed-mount`'s Deployment-level `UNSATISFIED_REPLICAS`
incident and its own Pod-level `FAILED_MOUNT` one), and the harness's
first version took whichever the repository listed first rather than the
one whose diagnosis actually matched -- fixed to keep polling for an
exact match until the deadline, reconfirmed live afterward.

## Milestone 8: Kubernetes operator -- DONE

Scoped down to one CRD, `KubeSentinelRemediationPolicy` (see
`docs/OPERATOR_DESIGN.md` §1 for why a second, more generic
`KubeSentinelPolicy` had no concrete content left to carry). The
controller never reimplements `PolicyEngine`'s decision logic, and -- the
load-bearing constraint, stated explicitly in the design doc before any
code -- never decides which `ActionType` to propose for an incident
either; that remains entirely human/CLI/API-driven, exactly as before.
It only re-evaluates an already-proposed, still-`PENDING_APPROVAL` action
against the policy a CR currently declares, calling the exact same
`PolicyEngine.evaluate()`/`RemediationService.approve()`/`.execute()`
every other caller already uses. Defaults to observation (no CR for a
namespace means no reconciliation there at all); any actual remediation
still goes through the same `approve()` call a human would make. No
`kopf`/`kubebuilder` dependency -- reuses this project's own proven
list-then-watch loop (`observer/watchers.py`) via a new, seventh,
dict-returning watcher, confirmed live against a real CRD before writing
either the mode or any code depending on it.

Live-verified against the real cluster: RBAC confirmed against the real
constrained ServiceAccount (not admin), a real `crashloop-demo` incident
correctly left `PENDING_APPROVAL` under `CONTROLLED_AUTO` (its diagnosing
rule forbids auto-remediation, by design, like every rule in this
codebase today), and -- after temporarily marking a copy eligible and
clearing the single-replica safety check -- a genuine auto-approved,
auto-executed real Deployment restart, with `approved_by:
kubesentinel-operator` in the audit trail and an honest
`VERIFICATION_INCONCLUSIVE` outcome (correctly not claiming success for a
restart that never fixes a deterministic crash). This same live run
caught and fixed a real bug: the controller's own re-evaluation of a
pending action initially included that action in its own recent-actions
cooldown check, self-denying on a 0-second-old cooldown against itself.

## Milestone 9: Final integration and portfolio demonstration -- DONE

Fresh local setup verified from a real clean clone (`git clone` into a
scratch directory, not just a read-through): `check-prereqs.sh`,
`setup.sh`, the fresh venv's own `./scripts/test.sh` (774 passed), and
`./lab/create-cluster.sh` against the existing cluster (safe -- both
`kind` and `kubectl apply` are idempotent, confirmed by reading the
script before relying on that) all worked exactly as documented, with
every CLI command's `--help` output spot-checked against README's own
literal examples (`review-recommendation`, `dry-run`, `plan`,
`schedule-check`, `network-check`, `evaluate`) and found to match.

This pass still caught exactly the kind of drift the roadmap predicted,
just not where expected -- not a wrong flag, but *coverage* drift: two
capabilities built in Milestones 7 and 8 (the evaluation harness, the
operator) existed in code and in their own design docs, but had never
been added to `README.md`'s Quickstart, its Project layout tree, or
`DEMO_SCRIPT.md` at all. Fixed: `DEMO_SCRIPT.md` gained new steps 8g
(evaluation harness) and 8h (operator), both run live against the real
cluster to confirm every command in them actually works as written
before committing the doc (not assumed); its stale "There's no AI layer
yet (Milestone 11, optional)" line -- wrong, since AI summarization has
in fact shipped and is documented elsewhere in this same file -- was
corrected. `README.md`'s Quickstart, Project layout, and documentation
index were all updated to mention both capabilities.

## Sequencing notes

Per the spec's own final instruction ("begin with whichever prerequisite
is genuinely incomplete"): Milestone 1 is already substantially satisfied
by the audit, and Milestone 3 (scheduling advisor) is now done -- chosen
first over 4/5/6 because it had the most existing code to build on
(`scheduling.py`'s message classification, already reused once) and the
clearest, most self-contained scope. That reasoning held: the whole
milestone shipped in one pass with no scope renegotiation needed mid-build.

Milestones 4, 5, and 6 still have no hard dependency on each other or on 3,
beyond all four sharing the `MASTER_ARCHITECTURE.md` pattern (now proven
against a third capability, not just a second -- see
`IMPLEMENTATION_STATUS.md`'s "Shared observation, storage, and API"
section) -- their relative order is a scheduling convenience, not a
technical requirement, and can be revisited. Capability D (network) is a
reasonable next pick: it's the one most directly promised by Capability A's
own long-reserved, long-unused `DNS_RESOLUTION_FAILURE` reason code, and it
has a real environment constraint (kindnet not enforcing NetworkPolicy)
already on record to design around rather than discover mid-build, the same
way Milestone 3's single-node constraint was known going in.

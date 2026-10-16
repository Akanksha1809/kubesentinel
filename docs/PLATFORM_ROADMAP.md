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

## Milestone 1: Stabilize the existing core

**Scope, adapted to what the audit actually found**: the spec frames this
as "ensure the observer, troubleshooting engine, incident model, audit
layer, and safe remediation workflow work reliably" -- the audit found all
of that already true, tested, and live-verified (410 unit tests, 32
integration/e2e tests covering exactly this, RBAC boundary enforcement,
concurrent-remediation correctness, recovery verification). There is no
large stabilization backlog to work through here; inventing one would
contradict the spec's own instruction not to rewrite working components
without a concrete reason.

What the audit did surface as small, real, named gaps -- optional
hardening, not blocking, and each genuinely small enough to fit inside
this milestone without ballooning it:

1. **`UsageSummary.throttled` is scaffolded but never populated** (always
   `None`) -- either implement the real
   `container_cpu_cfs_throttled_seconds_total` Prometheus query and
   populate it, or remove the field and let the near-limit `risks` text
   stand on its own without implying a metric that isn't actually read.
   Leaving a field that looks populated-when-available but is silently
   always empty is the kind of gap this project's own ethos (`never
   fabricate, be honest about absence`) argues for resolving one way or
   the other, not leaving ambiguous.
2. **No in-Pod mount-failure diagnostic rule** -- `pvc.py` covers binding
   failures only; a Pod stuck on `FailedMount` after its PVC is already
   Bound is currently invisible to this engine. Small, scoped addition if
   picked up (one new rule, one new lab scenario).
3. **No cross-link from a resource-optimization finding back into an open
   incident's own hypothesis list** -- today the link is one-directional
   (`ACTIVE_INCIDENT` on a recommendation). Whether this is worth building
   (e.g. an OOM incident's hypothesis explicitly citing "this container has
   no memory limit configured, per recommendation `<id>`") is a genuine
   product judgment call, not an obvious gap -- noted here for a decision,
   not pre-decided.

**Acceptance criteria**: full test suite still green after any of the
above; no change to existing passing tests' assertions (a hardening pass
that breaks an existing guarantee is not hardening). If none of the three
items above are picked up, this milestone closes immediately with "audit
confirmed already stable" as its own result -- a legitimate, honest
outcome, not a skipped milestone.

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
already-large static-analysis milestone. `EvidenceKind.SERVICE_SPEC` and
`ReasonCode.DNS_RESOLUTION_FAILURE` remain reserved-but-unproduced for the
same reason. **Ingress/Gateway**: confirmed live (`kubectl get
ingressclass` / `kubectl get crd`) that zero such resources or CRDs exist
anywhere in this project's cluster or manifest history -- an honest "not
applicable," not a built-but-silent gap.

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
workloads. **Deliberately deferred**: CVE/image vulnerability scanning (no
maintained vulnerability database this project has access to), Pod
Security Admission namespace-label auditing (redundant with the
already-built, more granular container-level checks), ServiceAccount
token automount auditing, and any compliance-framework scoring -- each
named explicitly as a gap, not silently absent.

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

## Milestone 7: Chaos and resilience lab

**Scope**: not new failure scenarios (11 already exist and are
live-verified -- see FAILURE_LAB.md) but the **evaluation harness** named
as missing in `IMPLEMENTATION_STATUS.md`: an automated way to apply a
scenario, measure time-to-detection/time-to-diagnosis/recovery-time/
false-positive rate against it, and persist that measurement for later
comparison -- turning the manually-observed, prose-only numbers scattered
through TODO.md's history into something a later run can reproduce and
diff against. Covers every analysis engine that exists by the time this
milestone starts (A, B, and whichever of C/D/E/F are already built),
not just the original troubleshooting scenarios.

**Explicitly bounded**: measured values only, never illustrative or
estimated numbers presented as measured -- matching this entire project's
existing discipline (TODO.md's own "live-verified, not asserted" standard)
extended to this harness specifically.

## Milestone 8: Kubernetes operator

**Scope**: one or more CRDs (`KubeSentinelPolicy` and/or
`KubeSentinelRemediationPolicy`, per the spec's own suggested names, scoped
down if their value isn't concrete enough to justify both) plus a thin
reconciliation controller. Per the spec's own explicit instruction, and
`MASTER_ARCHITECTURE.md`'s own policy-boundary section: the controller
calls into the *existing* `PolicyEngine`/service-layer interfaces, never
reimplements their logic a second time in Go or a second Python process
duplicating FastAPI's own business logic. Defaults to observation and
recommendation; any actual remediation triggered through the operator
still goes through the same approval mechanism everything else does.

**Sequenced last among the analysis-engine milestones on purpose**: the
operator exposes policy over capabilities that need to already exist and
be stable to be worth exposing this way.

**Design-first**: `docs/OPERATOR_DESIGN.md` before code.

## Milestone 9: Final integration and portfolio demonstration

**Scope**: full regression suite across every capability built by this
point, a fresh local setup verification from a clean clone (catches the
exact kind of documentation drift Milestone 19's own doc pass found twice
in the prior extension -- a misleading CLI flag, a truncated ID a command
doesn't actually accept), and a coherent end-to-end demo spanning every
capability, following the precedent already set by `DEMO_SCRIPT.md`'s
existing step 8b (resource optimization) and the real, recorded (not
staged) demo GIF now in `README.md`.

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

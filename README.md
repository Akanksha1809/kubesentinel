# KubeSentinel

A Kubernetes operations platform you run entirely on your own machine against a
real local [kind](https://kind.sigs.k8s.io/) cluster -- no paid cloud services,
no required AI model, no managed control plane. It's built to demonstrate real
Kubernetes engineering depth: control-loop semantics (watch/relist/resync, not
polling), the difference between an *observed* fact and an *inferred*
hypothesis, a safety-gated remediation policy, and recovery that's *verified*
against the live cluster rather than assumed from an action's own "success"
response -- not a generated README wrapped around an LLM call.

See [DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) for a full guided walkthrough and
[TODO.md](TODO.md) for the detailed build history and exactly what is and
isn't built, with live-verification evidence for every claim.

## Demo

![KubeSentinel demo: diagnosis, policy-controlled remediation, and resource optimization recommendations, all against a real kind cluster](docs/images/demo.gif)

A real, unedited terminal session against a real kind cluster: what
Kubernetes itself reports vs. what KubeSentinel diagnoses, a full
policy-controlled `plan -> approve -> execute -> verify` remediation
(correctly reporting `VERIFICATION_FAILED` rather than lying about an action
that didn't fix anything), and the resource optimization engine producing a
real `CPU_OVERSIZED`/`MEMORY_OVERSIZED` recommendation from real
metrics-server samples. Recorded with [asciinema](https://asciinema.org) +
[agg](https://github.com/asciinema/agg) -- every line is genuine command
output, not staged. Running `./scripts/demo.sh` yourself (see Quickstart,
below) reproduces the same kind of session live, including the
`transient-recovery-demo` Pod opening and then resolving a real incident with
a measured `recovery_duration_seconds`, with nobody polling for it by hand.

## What this is, and how it works

The platform spec it was built against names six independent capabilities
(A-F below), plus a chaos/resilience evaluation harness and a Kubernetes
operator. All eight are built, tested, and live-verified against a real
cluster -- every claim in this document is backed by a specific file, a
specific test, or a specific command run against the actual
`kind-kubesentinel` cluster, which is this project's own standard for itself
(see [IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md), written so
every "implemented" claim can be checked against the repository rather than
taken on faith).

Three design commitments run through every engine, with no exceptions:

1. **Evidence is a first-class object, not a side effect of diagnosis.**
   Every fact a rule uses is a typed `Evidence` object naming exactly which
   field of the real Kubernetes API response it came from. A diagnostic rule
   never reads a raw `V1Pod`/`V1Deployment` object directly -- it only ever
   sees `list[Evidence]`. This is what makes "distinguish observed facts from
   inferred hypotheses" an *enforceable* property instead of a design
   aspiration: a rule that wants to claim something has to point at a
   specific evidence id, and anything it couldn't verify goes into
   `missing_evidence` instead of being silently assumed. Confidence is never
   "proven" -- only `HIGH`/`MEDIUM`/`LOW`, always with a citation.
2. **Everything that touches the cluster is read-only by default, and
   explicitly gated when it isn't.** Five of the six engines (B through F)
   never mutate the cluster at all -- read-only *by construction*, confirmed
   with a live test that patches the HTTP transport layer itself and asserts
   an entire analysis pass issues nothing but `GET` requests. The one engine
   that does act on the cluster (A, troubleshooting) only ever does so
   through a `plan -> approve -> execute` lifecycle gated by a policy engine
   (modes: `OBSERVE` / `APPROVAL_REQUIRED` / `CONTROLLED_AUTO`), and nothing
   in the live diagnosis pipeline *automatically* triggers it -- every
   remediation is a deliberate human action, or, in `CONTROLLED_AUTO` mode,
   an explicit policy decision, never an unattended reaction to a diagnosis.
3. **An action's own reported success is never trusted.** A recovery
   verifier polls the live cluster *after* every remediation action and
   checks for the actual symptom's absence, rather than trusting the
   executor's "it returned 200." The demo above shows this live: a restart
   genuinely executes, but is correctly reported as `VERIFICATION_FAILED`
   because the underlying readiness-probe failure was never something a
   restart could fix -- the system refuses to lie about an outcome it can
   check.

## The platform

```
Observer (watch Pods, Events, Deployments,  -->  Evidence extraction  -->  Diagnostic
PVCs, Nodes, EndpointSlices)                                                engine (rules)
                                                                                 |
                                                                                 v
                                               Incident manager (dedup by resource UID,
                                                      any kind -- Pod/Deployment/      |
                                                      PVC/Node/Service)                v
                                                            |                 Policy engine -> Remediation
                                                            v                 executor -> Recovery verifier
                                            SQLite persistence  <-->  FastAPI + CLI + /metrics
```

**Capability A -- Intelligent Troubleshooting & Self-Healing.** Watches
Pods, Events, Deployments, PVCs, Nodes, and EndpointSlices live and runs 13
deterministic diagnostic rules over the evidence they produce, covering
CrashLoopBackOff, OOMKilled (HIGH confidence when the kubelet reports the
reason directly, MEDIUM and explicitly labeled *inferred* when it only shows
up as exit code 137), image-pull failures, pending-pod scheduling, Deployment
rollout failures, readiness/liveness probe failures, Service selector
mismatches, PVC binding failures, in-Pod volume mount failures, Node
pressure, and missing ConfigMap/Secret references. (DNS/connectivity
diagnosis is deliberately not a 14th rule here -- see Capability D.) Incidents
are deduped by resource UID, not name+namespace, because Kubernetes names get
reused constantly and UID is the only identifier the API server guarantees is
unique across an object's entire lifetime; a real recovery is measured end to
end from the same UID going unhealthy-to-healthy, never guessed. Remediation
-- restart, rollback, or scale a Deployment -- is a full
`plan -> approve -> execute -> verify` lifecycle gated by a policy engine
(mode, namespace/resource allowlists, cooldowns, per-incident and
per-window budgets) and checked afterward by a recovery verifier that polls
the live cluster rather than trusting the executor's own report. Exposed
identically over a token-gated FastAPI surface and an equivalent CLI -- both
call the same service-layer methods, so there's only ever one implementation
of the decision logic to audit. See
[ARCHITECTURE.md](docs/ARCHITECTURE.md),
[DIAGNOSTIC_RULES.md](docs/DIAGNOSTIC_RULES.md), and
[REMEDIATION_SAFETY.md](docs/REMEDIATION_SAFETY.md).

```
Workload inventory (owner-reference   -->  Metrics API / Prometheus  -->  Deterministic
resolution: Pod -> ReplicaSet ->            usage providers                analyzer (CPU/memory
Deployment, StatefulSet/DaemonSet/Job)                                     right-sizing, pressure,
                                                                            data-quality)
                                                                                 |
                                                                                 v
                                                      Safety checks (flags unhealthy/system/
                                                      conservative workloads + ACTIVE_INCIDENT,
                                                      never silently drops a row)
```

**Capability B -- Resource Optimization Engine.** A second, entirely
**read-only** pipeline sharing only the SQLite file and (optionally) the
incident repository with Capability A -- never a cluster watch loop or a
parallel incident concept. Resolves correct owner references
(Pod -> ReplicaSet -> Deployment, or direct for StatefulSet/DaemonSet/Job, or
standalone Pod) and right-sizes CPU and memory using deliberately different
statistics: CPU from a percentile of *typical* demand (default p95) plus a
safety margin, memory from observed *peak* (default p99), never the average
-- a transient average is dangerous for a memory limit in a way it isn't for
a CPU request. Never proposes a number from fewer than a configurable minimum
sample count and time window; a recorded OOM kill or confirmed CPU throttling
vetoes any proposed *decrease* but never triggers an automatic *increase*.
Also computes cluster-level overcommitment (requested-vs-allocatable,
limits-vs-allocatable, and real current-usage pressure, each a separate,
never-conflated ratio) and cross-links both directions with Capability A: a
recommendation carries `ACTIVE_INCIDENT` when an open troubleshooting
incident exists for the same workload, and an `OOM_KILLED` hypothesis links
back to any open memory recommendation for it. An apply path exists and
reuses Capability A's exact policy/execution/verification lifecycle rather
than duplicating it -- it still always requires explicit human approval and
patches only the specific resource keys a recommendation actually proposed.
See [RESOURCE_OPTIMIZATION_DESIGN.md](docs/RESOURCE_OPTIMIZATION_DESIGN.md).

A third capability, **Capacity Planning & Autoscaling Advisor (E)**, extends
this same pipeline -- additive work inside the resource optimization engine
rather than a new top-level package, since the platform spec explicitly asks
not to build a second metrics collector. It reads a workload's real
`HorizontalPodAutoscaler` status (current/desired replicas, every metric's
current value vs. target, the real `ScalingLimited` condition), flags when
an HPA's utilization target is meaningless because the container has no
corresponding resource request, and runs a coarse, explicitly-approximate
cluster-headroom check for the specific case of an HPA capped at its maximum
while still over target. Memory-leak-shaped and single-threaded-bottleneck
signals are reported as pattern matches over existing usage samples, always
labeled as such; an external dependency as the real bottleneck is named as a
hard limitation this project has no tracing/latency evidence for at all,
never silently assumed. No apply/write path exists here either. See
[CAPACITY_PLANNING.md](docs/CAPACITY_PLANNING.md).

```
Node inventory (allocatable,          -->  Per-constraint evaluators    -->  Finding: per-node,
taints, labels) + a real or               (resource fit, taints,            per-constraint
hypothetical Pod spec                     node selector, required          pass/fail with real
                                           affinity, namespace quota)       arithmetic in the
                                                                            reasoning -- plus what
                                                                            wasn't evaluated
```

**Capability C -- Kubernetes Scheduling & Placement Advisor.** Given a real
Pending Pod or a hypothetical spec, independently reconstructs *why* a Node
does or doesn't fit, one constraint at a time: real Node allocatable-vs-
requested arithmetic (including the real scheduler algorithm for init
containers and Kubernetes-native sidecars), real taint/toleration matching,
real required node-affinity and node-selector evaluation, real required pod
affinity/anti-affinity and topology-spread evaluation against every other
already-scheduled Pod's labels, and real namespace-ResourceQuota/LimitRange
checking for a hypothetical spec. It's explicit about what it *doesn't*
evaluate too -- the preferred half of node/pod affinity, and any required
pod-affinity term using `namespaceSelector` -- detected and named, never
guessed at. A read-only `POST /scheduling/simulate` endpoint analyzes a raw
Pod spec with zero mutation; no Pod is ever actually created. See
[SCHEDULING_ANALYSIS.md](docs/SCHEDULING_ANALYSIS.md).

```
Service/Pod/EndpointSlice       -->  Independent selector/port       -->  Finding: classification
inventory + NetworkPolicy            matching, EndpointSlice             (healthy/config-error/
inventory + CoreDNS/kube-dns         consistency check, DNS             unready-endpoints/
health (read-only, kube-system       health, applicable                 dns-unhealthy/policy-
RBAC-scoped to those two names)      NetworkPolicy summaries            may-restrict/inconclusive)
```

**Capability D -- Kubernetes Network Troubleshooting Engine.** Explains a
real Pod's inbound Service-based connectivity story: which Services actually
select it (computed independently from live Pod labels, not inferred from
EndpointSlice counts), whether its EndpointSlice output agrees, whether each
Service's `targetPort` actually resolves against the Pod's declared
container ports (numeric and named `targetPort`s have genuinely different
semantics, both implemented precisely), whether cluster DNS looks healthy
(a real, live read of CoreDNS/`kube-dns`, through a narrowly scoped RBAC
exception), and which NetworkPolicies apply -- always as static policy
analysis, never a claim about actually-observed blocked traffic (this
project's lab cluster uses kind's default CNI, which doesn't enforce
NetworkPolicy at all, so the strongest classification is `POLICY_MAY_RESTRICT`,
never "X is actually blocked"). Every finding's reasoning states the same
caveat: API inspection can describe configuration, never prove a packet
actually arrives -- which is exactly what the engine's one opt-in,
off-by-default extra closes: an **active connectivity probe** creates a
short-lived, auto-cleaned-up Job that performs a real DNS lookup and a real
TCP connect against a target Service. This is the project's first mutation
capability outside the fully policy-gated remediation executor, closed only
after its own dedicated safety review (its own namespace allowlist, cooldown,
concurrency limit, a fixed shell command caller input never reaches except
through typed environment variables, and a Pod template that passes this
project's own container-security checks by construction). See
[NETWORK_DIAGNOSTICS.md](docs/NETWORK_DIAGNOSTICS.md).

```
Pod/RBAC/NetworkPolicy/PDB   -->  Native rule catalog (container     -->  SecurityFinding(s):
inventory (one-shot,              security context, RBAC                severity, evidence,
Pods grouped by owning            over-permissiveness, NetworkPolicy     explanation, remediation
workload, reused owner             coverage, PDB coverage) --
resolution)                       no external scanner dependency
```

**Capability F -- Kubernetes Security & Configuration Auditor.** A native
rule catalog (no kube-linter/Kubescape dependency -- a deliberate
build-vs-integrate decision, consistent with every other engine being a
native, in-repo rule engine) covering container security context
(privileged, allowPrivilegeEscalation, runAsNonRoot, readOnlyRootFilesystem,
host namespace sharing, hostPath volumes, mutable image tags, Secrets as env
vars), RBAC over-permissiveness (cluster-admin bindings, wildcard
verb+resource rules, and a subject's *effective* permissions resolved across
every Role/ClusterRole binding naming it combined -- all excluding
Kubernetes' own built-in, unfixable bootstrap identities, confirmed live that
a real cluster has *two* such identities, not just the one better-known
one), NetworkPolicy coverage (reusing the network engine's own
classification rather than re-reading NetworkPolicy objects a second time),
PodDisruptionBudget coverage for multi-replica workloads, Pod Security
Admission namespace labeling, and opt-in CVE/image vulnerability scanning (a
local Trivy binary scans each unique container image -- the one check in
this engine that shells out to an external scanner rather than reading a
Kubernetes object directly, since that genuinely needs a maintained
vulnerability database this project has no intention of building). No
apply/write path exists here either. See
[SECURITY_AUDITING.md](docs/SECURITY_AUDITING.md).

A **chaos & resilience evaluation harness** drives all six engines above
against the real cluster and measures them: it deletes-then-reapplies a lab
scenario for a genuine, verified-absent timing baseline, then records real
time-to-detection/time-to-diagnosis (the push-based troubleshooting engine
via its own short-lived observer; the five pull-based engines by
re-triggering analysis each poll), recovery time where a scenario defines
one, and a whole-battery false-positive rate against every currently-open
incident/finding that can't be attributed to a registered scenario.
`kubesentinel evaluate` is CLI-only, deliberately never an HTTP endpoint,
since a run mutates the shared lab namespace and can take minutes. See
[CHAOS_RESILIENCE_LAB.md](docs/CHAOS_RESILIENCE_LAB.md).

A **Kubernetes operator** exposes remediation policy declaratively: one CRD,
`KubeSentinelRemediationPolicy` (`crds/kubesentinelremediationpolicy.yaml`),
reconciled by a thin controller (`kubesentinel operator-run`) that never
decides which remediation action to propose for an incident -- that stays
entirely human/CLI/API-driven, exactly as in Capability A -- and only
re-evaluates an already-proposed, still-pending action against the policy
mode (`OBSERVE`/`APPROVAL_REQUIRED`/`CONTROLLED_AUTO`) a CR currently declares
for its own namespace, auto-approving and executing through the exact same
policy/execution calls a human would otherwise make by hand. No CRD for a
namespace means no reconciliation there at all. See
[OPERATOR_DESIGN.md](docs/OPERATOR_DESIGN.md).

Every stage above is exported as Prometheus metrics with bounded-cardinality
labels (category/action_type/reason/component, never incident IDs or Pod
names) at `GET /metrics`. [observability/](observability/) has a scrape
config and an importable Grafana dashboard, both verified against the real
server's own `/metrics` output. Optional AI summarization
(`kubesentinel explain <incident-id>`, or `GET /incidents/{id}/ai-summary`)
can turn an incident's existing diagnosis into a plain-English explanation --
strictly advisory, never a new hypothesis or a remediation decision. The only
backend is a local [Ollama](https://ollama.com) server: no API key, no cloud
account, no per-request cost, genuinely free rather than "free tier."
Disabled by default; every feature above works fully without it. See
[RBAC_AND_THREAT_MODEL.md](docs/RBAC_AND_THREAT_MODEL.md) for why it has no
path back into any decision.

16 lab failure scenarios exist, one dedicated manifest each, across every
capability (see [FAILURE_LAB.md](docs/FAILURE_LAB.md)). The test suite
includes integration and e2e tests that exercise the real cluster directly
(RBAC boundary enforcement against a real non-admin ServiceAccount token,
watch resync against a live API server, UID-based dedup across a real
delete/recreate, restart recovery, 10 real concurrent threads racing a
remediation proposal on the same incident, the full
propose/approve/execute/verify lifecycle against a real Deployment, and each
read-only engine's own independent math reaching the same conclusion the real
cluster/scheduler/DNS/HPA already reached) -- `./scripts/test.sh --all` runs
the full suite (1,072 unit tests plus 76 integration/e2e tests, `ruff check`
and `mypy` both clean); see [TESTING.md](docs/TESTING.md).

## Quickstart

Prerequisites: Docker (or Colima), [kind](https://kind.sigs.k8s.io/), kubectl, Python 3.11+.

```bash
./scripts/check-prereqs.sh   # verify everything is installed and Docker is reachable
./scripts/setup.sh           # create .venv, install kubesentinel + dev deps, write .env
./lab/create-cluster.sh      # create the local kind cluster + kubesentinel-lab namespace
./scripts/test.sh            # ruff + mypy + unit tests (no cluster required)
./scripts/demo.sh            # start the API server, then apply failure scenarios
```

In a second terminal, while `./scripts/demo.sh` is running:

```bash
curl -s http://127.0.0.1:8000/incidents | python3 -m json.tool
.venv/bin/kubesentinel incidents
.venv/bin/kubesentinel diagnose kubesentinel-lab crashloop-demo-<pod-suffix>
.venv/bin/kubesentinel evidence <incident-id>
curl -s http://127.0.0.1:8000/metrics | grep kubesentinel_
```

Remediation defaults to `OBSERVE` mode (diagnose-only, nothing is ever executed)
until `KUBESENTINEL_POLICY_MODE` is set, and mutating API endpoints are disabled
(`503`) until `KUBESENTINEL_API_TOKEN` is set -- see
[REMEDIATION_SAFETY.md](docs/REMEDIATION_SAFETY.md). With those set, both
surfaces expose the same `plan -> approve -> execute -> reject` lifecycle:

```bash
.venv/bin/kubesentinel dry-run <incident-id> RESTART_DEPLOYMENT
.venv/bin/kubesentinel plan <incident-id> RESTART_DEPLOYMENT --rationale "manual override"
.venv/bin/kubesentinel approve <incident-id> <action-id> --approved-by "<your name>"
.venv/bin/kubesentinel execute <incident-id> <action-id>
```

The resource optimization engine needs no extra setup to run -- without
metrics-server installed, it still produces honest, non-usage-based
recommendations (missing requests/limits, scheduling pressure). To see
CPU/memory right-sizing with real usage data too, install metrics-server
first (`./lab/install-metrics-server.sh`, optional, kind-specific):

```bash
.venv/bin/kubesentinel optimize --namespace kubesentinel-lab
.venv/bin/kubesentinel recommendations
.venv/bin/kubesentinel recommendation <recommendation-id>
.venv/bin/kubesentinel optimization-summary
.venv/bin/kubesentinel review-recommendation <recommendation-id> APPROVED --reviewed-by "<your name>"
```

The scheduling advisor explains a real Pending Pod, or simulates a
hypothetical spec that doesn't exist yet -- also no extra setup needed:

```bash
.venv/bin/kubesentinel schedule-check kubesentinel-lab <pod-name>
.venv/bin/kubesentinel scheduling-findings
.venv/bin/kubesentinel scheduling-finding <finding-id>
```

The primary kind cluster is single-node, so a live demo there can only
ever show one Node's own pass/fail, never "fits Node B, not Node A." To
see a genuine cross-node trade-off, create a second, separate, optional
cluster (1 control-plane + 2 workers, never touches the primary one):

```bash
./lab/create-multi-node-cluster.sh    # switches kubectl's current context to it
# ...run a scheduling simulation against it, e.g. label one worker and
# simulate a Pod with a matching nodeSelector...
./lab/delete-multi-node-cluster.sh    # tears it down
kubectl config use-context kind-kubesentinel   # back to the primary cluster
```

The network troubleshooting engine explains a real Pod's inbound
Service-based connectivity story -- also no extra setup needed:

```bash
.venv/bin/kubesentinel network-check kubesentinel-lab <pod-name>
.venv/bin/kubesentinel network-findings
.venv/bin/kubesentinel network-finding <finding-id>
```

Capacity/HPA findings surface through the same optimization-engine
commands as CPU/MEMORY/SCHEDULING, under a new category:

```bash
.venv/bin/kubesentinel optimize --namespace kubesentinel-lab
.venv/bin/kubesentinel recommendations --category CAPACITY
.venv/bin/kubesentinel recommendation <recommendation-id>
```

The security & configuration auditor runs a cluster-wide audit -- also
no extra setup needed:

```bash
.venv/bin/kubesentinel security-audit --namespace kubesentinel-lab
.venv/bin/kubesentinel security-findings --severity CRITICAL
.venv/bin/kubesentinel security-finding <finding-id>
```

The chaos & resilience evaluation harness measures any lab scenario
against the real cluster -- mutates the shared lab namespace (deletes
then reapplies the scenario touched), so it's CLI-only and deliberately
not run automatically:

```bash
.venv/bin/kubesentinel evaluate --scenario failed-mount
.venv/bin/kubesentinel evaluation-runs
```

The Kubernetes operator reconciles a `KubeSentinelRemediationPolicy` CR
declaratively -- it never decides which action fixes an incident (that
stays entirely CLI/API-driven via `plan`), only auto-approves and
executes an already-proposed, still-pending action once the CR's own
policy allows it:

```bash
kubectl apply -f crds/kubesentinelremediationpolicy.yaml
.venv/bin/kubesentinel operator-run
```

AI summarization is off by default (works with no AI at all). To try it:
[install Ollama](https://ollama.com), `ollama pull qwen2.5-coder:3b` (or any
model you have), then:

```bash
KUBESENTINEL_AI_ENABLED=true .venv/bin/kubesentinel explain <incident-id>
# or restart ./scripts/demo.sh with KUBESENTINEL_AI_ENABLED=true and:
curl -s http://127.0.0.1:8000/incidents/<incident-id>/ai-summary
```

When done: `Ctrl+C` the demo server, then `./lab/delete-cluster.sh` to tear the
cluster down (destructive, scoped only to the `kubesentinel` kind cluster).

## Image caching note for kind on macOS/Colima

If your Docker daemon's outbound network can't reach Docker Hub (seen in this
project's own dev environment -- confirmed via `docker pull` timing out while the
host's own network could reach the registry directly), pre-load images into the kind
node instead of relying on an in-cluster pull:

```bash
docker pull busybox:latest        # or any image already cached on the host
docker save busybox:latest -o /tmp/busybox.tar
docker cp /tmp/busybox.tar kubesentinel-control-plane:/root/busybox.tar
docker exec kubesentinel-control-plane ctr --namespace=k8s.io images import --digests --snapshotter=overlayfs /root/busybox.tar
```

Also make sure any manifest using a mutable tag like `:latest` sets
`imagePullPolicy: IfNotPresent` -- Kubernetes defaults `:latest` to `imagePullPolicy:
Always`, which forces a network round-trip on every pod start even when the image is
already cached locally. This bit us directly while building the lab scenarios; see
[TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Project layout

```
src/kubesentinel/
  models/        Pydantic models: Evidence, Hypothesis, DiagnosisResult, Incident, Action,
                 and every other engine's own models (WorkloadSnapshot/ResourceRecommendation,
                 SchedulingFinding, NetworkFinding, SecurityFinding, ...)
  observer/      Kubernetes client setup, watch loops with reconnect/resync, evidence extraction
  diagnostics/   Deterministic rule-based diagnostic engine + individual rules
  incidents/     Incident lifecycle: dedup by resource UID, health classification, severity
  optimization/  Read-only resource right-sizing: inventory, metrics providers, analyzer, safety,
                 cluster overcommitment, and capacity.py (HPA status/scale-headroom reasoning)
  scheduling/    Read-only placement analysis: node inventory, per-constraint evaluators, analyzer
  network/       Read-only connectivity analysis: service/policy inventory, DNS health, analyzer,
                 and the opt-in active connectivity probe
  security/      Read-only security/config auditing: inventory, native rule catalog, engine
  evaluation/    Chaos & resilience evaluation harness: scenario registry + the harness itself
                 (drives all six engines above against the real cluster and measures them)
  operator/      Kubernetes operator: reconciles KubeSentinelRemediationPolicy CRs, never
                 decides which action to propose -- only auto-approves/executes one already
                 proposed, through the exact same policy/execution calls a human would make
  persistence/   Repository interfaces + SQLite implementations (incidents, optimization,
                 scheduling, network, security, evaluation)
  api/           FastAPI app (health/ready/incidents/evidence/actions/metrics/optimization/
                 scheduling/network/security/evaluation)
  cli/           Typer CLI mirroring the API for one-shot cluster inspection, remediation,
                 optimization, scheduling, network, security analysis, the evaluation
                 harness, and running the operator
  metrics/       Prometheus metric definitions
  policy/        Policy engine: OBSERVE/APPROVAL_REQUIRED/CONTROLLED_AUTO decisions
  remediation/   Remediation executor + service (propose/approve/execute/reject/dry_run)
  verification/  Recovery verifier: polls the real cluster, never trusts an action's own success
  ai/            Optional AI summarization (local Ollama only, advisory, no path to any decision)
lab/             kind cluster config, namespace, failure scenario manifests, scripts,
                 optional metrics-server installer
crds/            The one CustomResourceDefinition the operator reconciles
                 (KubeSentinelRemediationPolicy)
rbac/            Least-privilege Role/RoleBinding/ServiceAccount for everything implemented
                 (observer, remediator, optimizer, scheduling-advisor, network-advisor,
                 capacity-advisor, security-advisor, and operator Roles -- each a separate,
                 independently auditable file, with narrow documented cluster-scope exceptions
                 only where a capability genuinely needs one)
observability/   Prometheus scrape config + importable Grafana dashboard
tests/           unit/ (deterministic, no cluster required), integration/ and e2e/ (against
                 a real kind cluster, skip-safe on missing preconditions)
docs/            Architecture, diagnostic rules, RBAC/threat model, failure lab, every engine's
                 own design doc, testing strategy, demo script, and the full build history
```

## Documentation

- [MASTER_ARCHITECTURE.md](docs/MASTER_ARCHITECTURE.md) -- the platform-wide shape every analysis engine follows
- [IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) -- evidence-backed, capability-by-capability status audit
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) -- component design and data flow for the troubleshooting and resource-optimization engines
- [RESOURCE_OPTIMIZATION_DESIGN.md](docs/RESOURCE_OPTIMIZATION_DESIGN.md) -- the resource optimization engine's own design and algorithms
- [SCHEDULING_ANALYSIS.md](docs/SCHEDULING_ANALYSIS.md) -- the scheduling advisor's own design: what's evaluated for real vs. only detected
- [NETWORK_DIAGNOSTICS.md](docs/NETWORK_DIAGNOSTICS.md) -- the network troubleshooting engine's own design: evidence chain, scope split, what's deliberately deferred
- [CAPACITY_PLANNING.md](docs/CAPACITY_PLANNING.md) -- the capacity planning & autoscaling advisor's own design: HPA status, scale headroom, what's a pattern match vs. a diagnosis
- [SECURITY_AUDITING.md](docs/SECURITY_AUDITING.md) -- the security & configuration auditor's own design: the rule catalog, the build-vs-integrate decision, what's deliberately deferred
- [CHAOS_RESILIENCE_LAB.md](docs/CHAOS_RESILIENCE_LAB.md) -- the evaluation harness's own design: ground truth, push-vs-pull timing, recovery/false-positive scope
- [OPERATOR_DESIGN.md](docs/OPERATOR_DESIGN.md) -- the Kubernetes operator's own design: why one CRD, what the controller deliberately never decides, reconciliation step by step
- [EVIDENCE_COMPLETENESS.md](docs/EVIDENCE_COMPLETENESS.md) -- the shared `EvidenceCompleteness` vocabulary across capabilities
- [DIAGNOSTIC_RULES.md](docs/DIAGNOSTIC_RULES.md) -- every rule, its evidence, and its confidence logic
- [FAILURE_LAB.md](docs/FAILURE_LAB.md) -- each lab scenario: how it fails, what's expected, how to clean up
- [RBAC_AND_THREAT_MODEL.md](docs/RBAC_AND_THREAT_MODEL.md) -- least privilege, trust boundaries
- [REMEDIATION_SAFETY.md](docs/REMEDIATION_SAFETY.md) -- policy modes, allowlists, cooldowns, and live verification evidence
- [KUBERNETES_CONCEPTS.md](docs/KUBERNETES_CONCEPTS.md) -- the K8s concepts this project exercises, tied to code
- [DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) -- why SQLite, why UID-based dedup, why rules not ML, etc.
- [TESTING.md](docs/TESTING.md) -- test strategy and what's actually covered
- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) -- environment issues hit while building this, and fixes
- [DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) -- a guided walkthrough for showing this off
- [TODO.md](TODO.md) -- the full build history, verified completion status, known limitations

## License

[MIT](LICENSE)

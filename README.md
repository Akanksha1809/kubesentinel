# KubeSentinel

A locally runnable Kubernetes troubleshooting and controlled self-healing platform.
Built to demonstrate real Kubernetes engineering depth: control-loop semantics,
observed-vs-inferred diagnosis, safe remediation policy, and recovery verification --
not a generated README wrapped around an LLM call.

Runs entirely on a local [kind](https://kind.sigs.k8s.io/) cluster. No paid cloud
services, no required AI model. See [DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) for a
guided walkthrough and [TODO.md](TODO.md) for exactly what is and isn't built yet.

## Demo

![KubeSentinel demo: diagnosis, policy-controlled remediation, and resource optimization recommendations, all against a real kind cluster](docs/images/demo.gif)

A real, unedited terminal session against a real kind cluster: what Kubernetes
itself reports vs. what KubeSentinel diagnoses, a full policy-controlled
`plan -> approve -> execute -> verify` remediation (correctly reporting
`VERIFICATION_FAILED` rather than lying about an action that didn't fix
anything), and the resource optimization engine producing a real
`CPU_OVERSIZED`/`MEMORY_OVERSIZED` recommendation from real metrics-server
samples. Recorded with [asciinema](https://asciinema.org) + [agg](https://github.com/asciinema/agg) --
every line is genuine command output, not staged.

## Status: Milestones 1-11 (troubleshooting) + 12-19 (resource optimization) complete

The full **observe -> diagnose -> persist -> expose** pipeline is implemented,
tested, and verified end-to-end against a real kind cluster for 13 of the 14
diagnostic categories in the original spec (DNS/connectivity deliberately
deferred -- see [TODO.md](TODO.md)): CrashLoopBackOff, OOMKilled,
ImagePullBackOff/ErrImagePull, missing ConfigMap/Secret references, pending-pod
scheduling constraints, readiness and liveness probe failures, Deployment
rollout failures, unsatisfied replica counts, PVC binding failures, Node
unschedulability, and Service/EndpointSlice selector mismatches -- plus a
transient-failure recovery scenario proving the full observe-diagnose-recover
loop.

Policy-controlled remediation is also implemented and live-verified: a policy
engine (OBSERVE/APPROVAL_REQUIRED/CONTROLLED_AUTO modes), a remediation
executor (restart/rollback/scale a Deployment, plus recommend-only actions), a
recovery verifier that polls the real cluster rather than trusting an action's
own reported success, and a full `plan -> approve -> execute -> reject`
lifecycle exposed over both a token-gated FastAPI surface and an equivalent
CLI. See [REMEDIATION_SAFETY.md](docs/REMEDIATION_SAFETY.md) for what's
enforced and how it was verified, and [TODO.md](TODO.md) for the full
milestone plan and current limitations (nothing yet triggers remediation from
the live diagnosis pipeline without a human or an explicit policy-engine
decision in the loop).

A second, read-only **resource optimization engine** sits alongside the
troubleshooting pipeline above, sharing its cluster client, SQLite file, and
`/metrics` endpoint without duplicating any of it: a workload inventory with
correct owner-reference resolution (Pod -> ReplicaSet -> Deployment,
StatefulSet/DaemonSet/Job, or standalone Pod), current usage from the
Kubernetes Metrics API (with an optional Prometheus upgrade for real
historical percentiles), and deterministic CPU/memory right-sizing --
percentile-based for CPU, peak-based for memory, never from a single sample,
never silently skipping an unhealthy workload (it's flagged instead). An open
troubleshooting incident on the same workload blocks a proposed number and
links back to it. There is deliberately no apply/execute endpoint: every
recommendation is a reviewable record (`new -> reviewed/approved/rejected ->
expired`), and analysis never mutates the cluster -- confirmed live with an
HTTP-transport-level check that an entire `analyze()` pass issues nothing but
GET requests. See [RESOURCE_OPTIMIZATION_DESIGN.md](docs/RESOURCE_OPTIMIZATION_DESIGN.md)
for the full design and TODO.md's Milestones 12-19 for the build and
live-verification history.

Every stage above is exported as Prometheus metrics with bounded-cardinality
labels (category/action_type/reason/component, never incident IDs or pod
names) at `GET /metrics`. [observability/](observability/) has a scrape config
and an importable Grafana dashboard, both verified against the real server's
`/metrics` output -- which is also how a real gauge-accuracy bug
(`incidents_active` undercounting incidents that predated the current process)
was caught; see TODO.md's Known limitation #18.

All 9 lab failure scenarios (every diagnostic category this project implements,
one dedicated manifest each) plus 2 resource-optimization scenarios
(oversized requests, missing requests entirely) are built and live-verified
-- see [FAILURE_LAB.md](docs/FAILURE_LAB.md). The test suite now includes
integration and e2e tests that exercise the real cluster directly (RBAC
boundary enforcement, watch resync, UID-based dedup across a delete/recreate,
restart recovery, concurrent remediation requests, the full propose/approve/
execute/verify lifecycle against a real Deployment, and the optimization
engine's own read-only guarantee and real-usage-data right-sizing) --
`./scripts/test.sh --all` runs all 442 tests (410 unit + 32 integration/e2e);
see [TESTING.md](docs/TESTING.md).

Optional AI summarization (`kubesentinel explain <incident-id>`, or
`GET /incidents/{id}/ai-summary`) can turn an incident's existing diagnosis
into a plain-English explanation -- strictly advisory, never a new hypothesis
or a remediation decision. The only backend is a local [Ollama](https://ollama.com)
server: no API key, no cloud account, no per-request cost, genuinely free
rather than "free tier." Disabled by default; every feature above works fully
without it. See [RBAC_AND_THREAT_MODEL.md](docs/RBAC_AND_THREAT_MODEL.md) for
why it has no path back into any decision, and TODO.md's Milestone 11 entry
for live verification evidence.

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

A second, read-only pipeline runs alongside it, sharing only the SQLite file
and (optionally) the incident manager's own repository for the `ACTIVE_INCIDENT`
check -- never a second cluster watch loop or a parallel incident concept:

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
                                                            |
                                                            v
                                            SQLite persistence  <-->  FastAPI + CLI + /metrics
```

## Quickstart

Prerequisites: Docker (or Colima), [kind](https://kind.sigs.k8s.io/), kubectl, Python 3.11+.

```bash
./scripts/check-prereqs.sh   # verify everything is installed and Docker is reachable
./scripts/setup.sh           # create .venv, install kubesentinel + dev deps, write .env
./lab/create-cluster.sh      # create the local kind cluster + kubesentinel-lab namespace
./scripts/test.sh            # ruff + mypy + unit tests (no cluster required)
./scripts/demo.sh            # apply failure scenarios and run the API server live
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
                 and the optimization engine's WorkloadSnapshot/ResourceRecommendation
  observer/      Kubernetes client setup, watch loops with reconnect/resync, evidence extraction
  diagnostics/   Deterministic rule-based diagnostic engine + individual rules
  incidents/     Incident lifecycle: dedup by resource UID, health classification, severity
  optimization/  Read-only resource right-sizing: inventory, metrics providers, analyzer, safety
  persistence/   Repository interfaces + SQLite implementations (incidents, optimization)
  api/           FastAPI app (health/ready/incidents/evidence/actions/metrics/optimization)
  cli/           Typer CLI mirroring the API for one-shot cluster inspection, remediation, and optimization
  metrics/       Prometheus metric definitions
  policy/        Policy engine: OBSERVE/APPROVAL_REQUIRED/CONTROLLED_AUTO decisions
  remediation/   Remediation executor + service (propose/approve/execute/reject/dry_run)
  verification/  Recovery verifier: polls the real cluster, never trusts an action's own success
  ai/            Optional AI summarization (local Ollama only, advisory, no path to any decision)
lab/             kind cluster config, namespace, failure scenario manifests, scripts,
                 optional metrics-server installer
rbac/            Least-privilege Role/RoleBinding/ServiceAccount for what's implemented today
                 (observer, remediator, and optimizer Roles, each independently auditable)
observability/   Prometheus scrape config + importable Grafana dashboard
tests/unit/      Deterministic unit tests, no cluster required
docs/            Architecture, diagnostic rules, RBAC/threat model, failure lab, resource
                 optimization design, etc.
```

## Documentation

- [MASTER_ARCHITECTURE.md](docs/MASTER_ARCHITECTURE.md) -- the platform-wide shape every analysis engine follows, built or planned
- [PLATFORM_ROADMAP.md](docs/PLATFORM_ROADMAP.md) -- milestone plan for the scheduling/network/capacity/security/operator capabilities ahead
- [IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) -- evidence-backed, capability-by-capability status audit
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) -- component design and data flow for the two capabilities already built
- [RESOURCE_OPTIMIZATION_DESIGN.md](docs/RESOURCE_OPTIMIZATION_DESIGN.md) -- the resource optimization engine's own design, algorithms, and milestone plan
- [DIAGNOSTIC_RULES.md](docs/DIAGNOSTIC_RULES.md) -- every rule, its evidence, and its confidence logic
- [FAILURE_LAB.md](docs/FAILURE_LAB.md) -- each lab scenario: how it fails, what's expected, how to clean up
- [RBAC_AND_THREAT_MODEL.md](docs/RBAC_AND_THREAT_MODEL.md) -- least privilege, trust boundaries
- [REMEDIATION_SAFETY.md](docs/REMEDIATION_SAFETY.md) -- policy modes, allowlists, cooldowns, and live verification evidence
- [KUBERNETES_CONCEPTS.md](docs/KUBERNETES_CONCEPTS.md) -- the K8s concepts this project exercises, tied to code
- [DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) -- why SQLite, why UID-based dedup, why rules not ML, etc.
- [TESTING.md](docs/TESTING.md) -- test strategy and what's actually covered
- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) -- environment issues hit while building this, and fixes
- [DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) -- a guided walkthrough for showing this off
- [TODO.md](TODO.md) -- milestone plan, verified completion status, known limitations

## License

[MIT](LICENSE)

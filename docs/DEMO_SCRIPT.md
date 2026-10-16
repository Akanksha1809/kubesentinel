# Demo script

A guided walkthrough for showing KubeSentinel off, matching what's implemented
through Milestone 18 (observe/diagnose/persist/expose, policy-controlled
remediation, recovery verification, observability, and the read-only resource
optimization engine -- see README.md's status line for exactly what's left).
Takes about 10 minutes for the full walkthrough including remediation; stop
after step 7 for the original ~5-minute diagnosis-only version, or skip
straight to step 8b for just the optimization engine.

## Setup (once)

```bash
./scripts/check-prereqs.sh
./scripts/setup.sh
./lab/create-cluster.sh
```

## The walkthrough

**1. Show there's nothing wrong yet.**

```bash
kubectl -n kubesentinel-lab get pods
# (no pods yet)
```

**2. Start KubeSentinel watching the lab namespace, in one terminal.**

```bash
./scripts/demo.sh
```

This creates the cluster if needed, and will apply all nine scenarios for you if you
haven't already. Leave it running -- it's the API server.

**3. In a second terminal, break things on purpose.**

If `./scripts/demo.sh` already applied the scenarios, skip to step 4. Otherwise:

```bash
./lab/scripts/run-scenario.sh crashloop
./lab/scripts/run-scenario.sh oom
./lab/scripts/run-scenario.sh imagepull
./lab/scripts/run-scenario.sh pvc-binding
./lab/scripts/run-scenario.sh service-selector-mismatch
./lab/scripts/run-scenario.sh pending-resources
./lab/scripts/run-scenario.sh readiness-probe-failure
./lab/scripts/run-scenario.sh failed-rollout
kubectl apply -f lab/manifests/scenario-transient-recovery/pod.yaml
```

**4. Watch Kubernetes's own view of the damage.**

```bash
kubectl -n kubesentinel-lab get pods -w
```

Point out: `CrashLoopBackOff`, `ImagePullBackOff`, restart counts climbing. This is
the raw material -- say explicitly that a status string alone doesn't tell you *why*.

**5. Ask KubeSentinel what it thinks, without touching kubectl.**

```bash
.venv/bin/kubesentinel incidents
```

Pick an incident ID from the table, then:

```bash
.venv/bin/kubesentinel evidence <incident-id>
curl -s http://127.0.0.1:8000/incidents/<incident-id> | python3 -m json.tool
```

Narrate the OOM one specifically: it carries **two** hypotheses --
`CRASH_LOOP_BACKOFF` (HIGH, directly observed from the waiting reason) and
`OOM_KILLED` (MEDIUM, explicitly labeled as *inferred* from exit_code 137, because
this dev environment's kubelet doesn't reliably set the `OOMKilled` reason even
though the kill genuinely happened -- confirmable against the node's own cgroup
`memory.events`). This is the "distinguish observed facts from inferred hypotheses"
requirement, shown live, not asserted in a README.

**6. Show the metrics a real operator would actually watch.**

```bash
curl -s http://127.0.0.1:8000/metrics | grep kubesentinel_
```

Point out bounded label cardinality: `category`, `reason_code` -- never an incident
ID or Pod name as a label. Optionally, point `observability/prometheus.yml` at this
server and import `observability/grafana-dashboard.json` to show the same data as
an operator would actually watch it, rather than a raw `curl`:

```bash
docker run --rm -p 9090:9090 \
  -v "$(pwd)/observability/prometheus.yml:/etc/prometheus/prometheus.yml" \
  prom/prometheus
```

**7. Show recovery detection, live.**

The `transient-recovery-demo` Pod fails twice, then recovers on its own (same Pod
UID throughout -- it's simulating "a dependency wasn't ready yet, now it is").

```bash
kubectl -n kubesentinel-lab get pod transient-recovery-demo -w
# watch it go CrashLoopBackOff -> Running
```

Then:

```bash
.venv/bin/kubesentinel incidents --status RESOLVED
```

Point out `recovery_duration_seconds` -- a real measured value, not a guess, derived
from actually observing the same Pod UID go from unhealthy to healthy via the
Kubernetes API.

**8. Show policy-controlled remediation end to end, against a real incident.**

In the terminal running `./scripts/demo.sh`, stop it (Ctrl+C) and restart it
with remediation enabled:

```bash
KUBESENTINEL_API_TOKEN=demo-token KUBESENTINEL_POLICY_MODE=APPROVAL_REQUIRED ./scripts/demo.sh
```

In the second terminal, find the `failed-rollout-demo` Deployment's own
incident (its category is `ROLLOUT_FAILED`) and walk the full
plan -> approve -> execute -> verify lifecycle via the CLI (the API exposes
the identical lifecycle over HTTP, token-gated -- see REMEDIATION_SAFETY.md).
Action types are the enum's own uppercase value (`RESTART_DEPLOYMENT`, not
`restart_deployment`):

```bash
.venv/bin/kubesentinel incidents   # find the failed-rollout-demo Deployment's incident id
.venv/bin/kubesentinel dry-run <incident-id> RESTART_DEPLOYMENT
.venv/bin/kubesentinel plan <incident-id> RESTART_DEPLOYMENT --rationale "demo: human override"
.venv/bin/kubesentinel approve <incident-id> <action-id> --approved-by "<your name>"
.venv/bin/kubesentinel execute <incident-id> <action-id>
```

The last command's output is the real point of this step:
`status: VERIFICATION_FAILED` -- the restart genuinely executed (confirmed via
a fresh `resourceVersion` on the Deployment), but `RecoveryVerifier` polled the
live cluster afterward and found the exact same `ProgressDeadlineExceeded`
condition still there, because a restart was never going to fix a readiness
probe that always fails. This is the "never mark anything resolved on an
action that didn't succeed" requirement, demonstrated live, not asserted: the
policy engine correctly let a human override an action the diagnosing rule
itself didn't believe would help, and the verifier correctly refused to lie
about the outcome. `lab/manifests/remediation-test/deployment.yaml` is the
separate, deliberately-healthy fixture used to live-verify the *successful*
scale/restart/rollback paths (see REMEDIATION_SAFETY.md) -- it has nothing to
diagnose on its own, so demonstrating against it needs a manually-constructed
incident outside the normal CLI/API surface, which is why this walkthrough
uses a real diagnosed incident instead.

**8b. Show the resource optimization engine -- a second, read-only pipeline.**

This step is independent of everything above; it can also be run on its own
against a freshly created cluster. Apply the two dedicated scenarios and
(optionally) install metrics-server for real usage data:

```bash
./lab/scripts/run-scenario.sh oversized-resources   # requests 1000m/256Mi, actually uses ~0
./lab/scripts/run-scenario.sh missing-requests      # no resources block at all
./lab/install-metrics-server.sh                     # optional -- without it, usage-based
                                                      # checks stay honestly silent
```

Run the analysis and look at what it found:

```bash
.venv/bin/kubesentinel optimize --namespace kubesentinel-lab
.venv/bin/kubesentinel recommendations
```

Point out `missing-requests-demo`'s `DATA_QUALITY` row: it appears
immediately, with no proposed numbers, because there's no request to
compare usage against. If metrics-server is installed, `optimize` won't yet
show a proposed CPU/memory number for `oversized-resources-demo` either --
that's the "no aggressive downsizing from a single sample" guarantee,
visible live. Run `optimize` two or three more times, a couple of minutes
apart:

```bash
.venv/bin/kubesentinel optimize --namespace kubesentinel-lab
# wait ~2 minutes, repeat at least once more
.venv/bin/kubesentinel recommendations
```

Once at least 3 samples spanning 5 minutes have accumulated, `oversized-resources-demo`
gets real `CPU_OVERSIZED`/`MEMORY_OVERSIZED` recommendations proposing
values near the configured floors (10m CPU / 16Mi memory by default), with
`reasoning` citing the actual observed percentile against the actual
configured request -- genuine math against genuine metrics-server samples,
not a canned number. Show one in full, and the summary -- the table above
only prints the ID's first 8 characters, so grab the full one from
`recommendations --json` first:

```bash
.venv/bin/kubesentinel recommendation <full-recommendation-id>
.venv/bin/kubesentinel optimization-summary
```

If any lab incident (e.g. `oom-demo`, `pending-resources-demo`) is still
open, point out its recommendation carries `ACTIVE_INCIDENT` in
`safety_flags` and `related_incident_ids` pointing back at the real
incident, with no proposed number -- the optimization engine defers to the
troubleshooting engine rather than recommending a resize alongside an
unresolved failure. Then record a human decision (the CLI talks to the service directly, the
same way `approve`/`execute` in step 8 do -- `require_api_token` only
gates the *HTTP* route, `POST /optimization/recommendations/{id}/review`):

```bash
.venv/bin/kubesentinel review-recommendation <full-recommendation-id> APPROVED --reviewed-by "<your name>"
```

There is no apply/execute step after this -- a review is a decision record
only; see RESOURCE_OPTIMIZATION_DESIGN.md §6 for why that's deliberate, not
an oversight.

**8c. Show the scheduling advisor explain -- not just classify -- a real
Pending Pod.**

```bash
./lab/scripts/run-scenario.sh invalid-node-selector
kubectl -n kubesentinel-lab get pods -l app=invalid-node-selector-demo
```

Find the real Pod name, then:

```bash
.venv/bin/kubesentinel schedule-check kubesentinel-lab <pod-name>
```

Point out the per-constraint breakdown: `RESOURCE_FIT` and
`TAINT_TOLERATION` both pass (real arithmetic against the real Node's
allocatable capacity, not a guess), while `NODE_SELECTOR` fails, naming the
exact label that didn't match. Contrast this with the earlier
`pending-resources-demo` scenario (step 3) -- running the same command
against that Pod instead shows `RESOURCE_FIT` as the blocker, with
`NODE_SELECTOR` passing. The point: the troubleshooting engine's own
`PendingSchedulingRule` already tells you a Pod is unschedulable and
roughly why (from the scheduler's own message text); this engine
independently re-derives *which specific constraint*, with its own
arithmetic, not just a classification of what the scheduler already said.

Then show the read-only simulation path -- no Pod is ever created:

```bash
curl -s -X POST http://127.0.0.1:8000/scheduling/simulate -H "Content-Type: application/json" \
  -d '{"namespace":"kubesentinel-lab","pod_spec":{"containers":[{"name":"app","resources":{"requests":{"cpu":"100m","memory":"64Mi"}}}]}}' \
  | python3 -m json.tool
```

**8d. Show the network troubleshooting engine explain a real Service/Pod
connectivity problem -- not a guess, evidence.**

```bash
./lab/scripts/run-scenario.sh service-port-mismatch
kubectl -n kubesentinel-lab get pods -l app=port-mismatch-demo
```

Find the real Pod name, then:

```bash
.venv/bin/kubesentinel network-check kubesentinel-lab <pod-name>
```

Point out that the Service's selector genuinely matches this Pod and it's
Ready (unlike the older `service-selector-mismatch-demo` Service, which
matches nothing) -- the break is specifically the named `targetPort: web`
not resolving to any container port this Pod actually declares
(`http`), named explicitly in the reasoning. Contrast with
`kubesentinel network-check kubesentinel-lab <remediation-test pod-name>`,
which returns `INCONCLUSIVE` for a Pod no Service selects at all -- the
engine distinguishes "nothing to validate" from "validated and broken."
Also point out the DNS health line in the output: a real, live read of the
cluster's own `coredns`/`kube-dns` state, through a narrowly RBAC-scoped
exception (see RBAC_AND_THREAT_MODEL.md) -- and that every finding's
reasoning ends with the same caveat: this is evidence from the Kubernetes
API, never a confirmed end-to-end packet-delivery test.

```bash
curl -s -X POST http://127.0.0.1:8000/network/analyze -H "Content-Type: application/json" \
  -d '{"namespace":"kubesentinel-lab","pod_name":"<pod-name>"}' \
  | python3 -m json.tool
```

**9. Be upfront about what this isn't, yet.**

Nothing in the live diagnosis pipeline *automatically* calls `propose()` --
every remediation above was a deliberate human action (or, in
`CONTROLLED_AUTO` mode, the policy engine's own explicit decision), never an
unattended reaction to a diagnosis. The network engine reads live CoreDNS
health and NetworkPolicy objects, but deliberately never runs an active
connectivity probe -- that would be this project's first mutation surface
outside the fully policy-gated `RemediationExecutor`, and was scoped out of
this milestone for its own dedicated safety review rather than folded in;
see NETWORK_DIAGNOSTICS.md Section 2. There's no AI layer yet (Milestone 11,
optional). The resource optimization engine never applies a
recommendation itself -- every number it produces still needs a human (or a
future, explicitly-built policy-engine path) to act on it; see
RESOURCE_OPTIMIZATION_DESIGN.md's known limitations for what's deliberately
not built there (cluster-level overcommitment analysis, an apply/execute
path). The scheduling advisor never evaluates pod affinity/anti-affinity or
topology spread constraints -- it says so explicitly in every finding that
has one, rather than guessing; see SCHEDULING_ANALYSIS.md Section 2.
`TODO.md` lists exactly what's next and what's known-incomplete; pull
it up if asked "so what's missing."

## Cleanup

```bash
# Ctrl+C the demo.sh server
./lab/scripts/cleanup.sh     # removes scenario workloads, keeps the namespace/cluster
./lab/delete-cluster.sh      # or tear the whole kind cluster down
```

# Demo script

A guided walkthrough for showing KubeSentinel off, matching the complete
platform spec (Capabilities A-F, the chaos & resilience evaluation
harness, and the Kubernetes operator -- see README.md's status line).
Takes about 15 minutes for the full walkthrough including remediation;
stop after step 7 for the original ~5-minute diagnosis-only version, or
skip straight to step 8b for just the optimization engine.

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

**8e. Show the capacity planning & autoscaling advisor reason about a
real HPA -- status, not a guess.**

```bash
./lab/scripts/run-scenario.sh hpa-capacity-pressure
kubectl -n kubesentinel-lab get hpa hpa-capacity-pressure-demo -w   # wait for TARGETS to show a real % (metrics-server needs ~60s)
```

Once it shows something like `cpu: 55%/1%`:

```bash
.venv/bin/kubesentinel optimize --namespace kubesentinel-lab
.venv/bin/kubesentinel recommendations --category CAPACITY
.venv/bin/kubesentinel recommendation <recommendation-id>
```

Point out `HPA_AT_MAX`: this HPA is pinned at `minReplicas=maxReplicas=1`
and still over its (deliberately tiny) target -- read from the HPA's own
real status and `ScalingLimited` condition, never inferred. Then point
out `SCALING_WOULD_BE_UNSCHEDULABLE` if present: a coarse, cluster-wide
check against this lab's real Node allocatable-vs-requested numbers,
explicitly labeled as an approximation, not the scheduling advisor's own
per-constraint simulation -- and explicitly *environment-dependent*
(it's only there because this lab's other scenarios' own CPU requests
leave no room on this single-Node cluster; see CAPACITY_PLANNING.md §11).
Also worth noting: no code path here ever patches the HPA itself --
exactly as advisory as every other optimization-engine category.

```bash
curl -s "http://127.0.0.1:8000/optimization/recommendations?category=CAPACITY" | python3 -m json.tool
curl -s "http://127.0.0.1:8000/optimization/workloads?namespace=kubesentinel-lab" \
  | python3 -c "import json,sys; print(json.dumps(next(w for w in json.load(sys.stdin) if w['workload_name']=='hpa-capacity-pressure-demo')['hpa'], indent=2))"
```

The second command shows the live `HpaSnapshot` -- exposed automatically
on the existing `/optimization/workloads` route, no new endpoint needed.

**8f. Show the security & configuration auditor flag a real
over-permissive grant and a real privileged container.**

```bash
./lab/scripts/run-scenario.sh security-findings
.venv/bin/kubesentinel security-audit --namespace kubesentinel-lab
.venv/bin/kubesentinel security-findings --severity CRITICAL
```

Pick one of the two CRITICAL rows and show it in full:

```bash
.venv/bin/kubesentinel security-finding <finding-id>
```

Point out `PRIVILEGED_CONTAINER`: a real `securityContext.privileged:
true` on a real Pod, with the exact evidence and remediation text named.
Then point out `CLUSTER_ADMIN_BINDING`: a real `ClusterRoleBinding`
granting `cluster-admin` to a throwaway ServiceAccount -- and that the
cluster's own *real* built-in bootstrap bindings (`system:masters`,
`kubeadm:cluster-admins`) are never flagged, confirmed live, not assumed
(this is exactly the kind of "known built-in, never actionable" exclusion
the scheduling/network engines also apply to their own domains). Worth
noting out loud: the rule catalog here is entirely native -- no
kube-linter/Kubescape dependency -- a deliberate build-vs-integrate
decision, not an oversight; see SECURITY_AUDITING.md Section 2.

```bash
curl -s -X POST http://127.0.0.1:8000/security/audit -H "Content-Type: application/json" \
  -d '{"namespaces":["kubesentinel-lab"]}' | python3 -c "import json,sys; print(len(json.load(sys.stdin)['findings']), 'findings')"
curl -s "http://127.0.0.1:8000/security/findings?severity=CRITICAL" | python3 -m json.tool
```

**8g. Show the chaos & resilience evaluation harness measure a real
scenario, not an estimate.**

```bash
.venv/bin/kubesentinel evaluate --scenario failed-mount --json
```

Point out the three timing fields: `time_to_detection_seconds` and
`time_to_diagnosis_seconds` read the same, real wall-clock number for
this scenario (push-based troubleshooting diagnoses inline the instant
the watcher sees the Event -- there is no separate detect-then-diagnose
phase in this architecture, and the harness reports that honestly rather
than inventing a gap to look more interesting); `recovery_time_seconds`
is `null` (this scenario has no self-healing path -- it's deliberately
left broken for inspection, the same as 15 of the lab's 16 scenarios).
Then show a past run and the whole-battery false-positive count:

```bash
.venv/bin/kubesentinel evaluation-runs
.venv/bin/kubesentinel evaluation-run <run-id>
```

Worth noting out loud: this mutates the shared lab namespace (deletes
then reapplies the scenario for a genuine, verified-absent timing
baseline) and is deliberately CLI-only, never an HTTP endpoint -- running
the full 16-scenario battery takes several minutes and is disruptive by
design, not something routine automation should trigger; see
CHAOS_RESILIENCE_LAB.md Section 6.

**8h. Show the Kubernetes operator reconcile a real policy CR -- without
ever deciding what to fix.**

```bash
kubectl apply -f crds/kubesentinelremediationpolicy.yaml
cat <<'EOF' | kubectl apply -f -
apiVersion: kubesentinel.io/v1alpha1
kind: KubeSentinelRemediationPolicy
metadata:
  name: lab-policy
  namespace: kubesentinel-lab
spec:
  mode: CONTROLLED_AUTO
EOF
.venv/bin/kubesentinel operator-run
```

In a second terminal, propose a real action against a real incident
(e.g. the `failed-rollout-demo` Deployment's own `ROLLBACK_DEPLOYMENT`-
eligible incident from step 8) and leave it `PENDING_APPROVAL`, then
watch the operator's own log line when it reconciles. Point out: the
controller never decided which `ActionType` to propose -- that's the
`plan` call a human (or this script) already made -- it only
re-evaluated the already-proposed action against the CR's own declared
policy, through the exact same `PolicyEngine.evaluate()`/
`RemediationService.approve()`/`.execute()` every other caller already
uses. Then check the CR's own status, read the Kubernetes-native way:

```bash
kubectl get ksrp lab-policy -n kubesentinel-lab -o yaml
```

Worth noting out loud: no rule in this project's diagnostic engine ever
sets `auto_remediation_permitted=True` today (a deliberate, conservative,
already-shipped decision) -- so a real diagnosis-driven incident will
correctly stay `PENDING_APPROVAL` even under `CONTROLLED_AUTO`, exactly
like it does for a human using the CLI directly. See
OPERATOR_DESIGN.md Section 2 for why that's the load-bearing constraint
behind this whole capability, not a limitation to apologize for.

**9. Be upfront about what this isn't, yet.**

Nothing in the live diagnosis pipeline *automatically* calls `propose()` --
every remediation above was a deliberate human action (or, in
`CONTROLLED_AUTO` mode, the policy engine's own explicit decision), never an
unattended reaction to a diagnosis -- and that remains true with the
operator running too: it only ever re-evaluates and auto-approves an
already-proposed action, never invents which action to propose. The network
engine reads live CoreDNS
health and NetworkPolicy objects, but deliberately never runs an active
connectivity probe -- that would be this project's first mutation surface
outside the fully policy-gated `RemediationExecutor`, and was scoped out of
this milestone for its own dedicated safety review rather than folded in;
see NETWORK_DIAGNOSTICS.md Section 2. The capacity planning advisor has no
visibility into external dependencies/latency at all (no tracing/APM data
exists in this project) -- every finding says so explicitly rather than
guessing at the cause of a bottleneck it can't actually see; see
CAPACITY_PLANNING.md Section 2. The security auditor doesn't do CVE/image
vulnerability scanning (no maintained vulnerability database this
project has access to) and doesn't resolve a subject's *effective*
permissions across every Role it's bound to combined -- it checks direct
bindings and rules only; see SECURITY_AUDITING.md Section 3/12. AI
summarization (`.venv/bin/kubesentinel explain <incident-id>`, step 5
could optionally show it) is real but deliberately narrow: advisory
only, adds no evidence, changes no diagnosis, and has no path back into
any decision -- it only ever explains an *already-computed* finding in
plain English, the same bounded role an optional local AI layer plays
everywhere else in this project; see RBAC_AND_THREAT_MODEL.md. The
chaos & resilience evaluation harness and the operator both stay
entirely human/CLI-driven too -- neither one ever decides what's broken
or which action fixes it (see steps 8g/8h above). The resource
optimization engine never applies a recommendation *on its own* -- every
number it produces still needs an explicit human `recommendation-apply` +
`action-approve` + `action-execute` sequence (RESOURCE_OPTIMIZATION_DESIGN.md
§14), the same `PolicyEngine`-gated lifecycle every other remediation
action already goes through; there is no unattended auto-apply path, and
none is planned. The scheduling advisor never evaluates pod affinity/anti-affinity or
topology spread constraints -- it says so explicitly in every finding that
has one, rather than guessing; see SCHEDULING_ANALYSIS.md Section 2.
`TODO.md` lists exactly what's next and what's known-incomplete; pull
it up if asked "so what's missing."

## Cleanup

```bash
# Ctrl+C the demo.sh server, and Ctrl+C the operator-run process from step 8h
kubectl delete ksrp lab-policy -n kubesentinel-lab --ignore-not-found
kubectl delete -f crds/kubesentinelremediationpolicy.yaml --ignore-not-found
./lab/scripts/cleanup.sh     # removes scenario workloads, keeps the namespace/cluster
./lab/delete-cluster.sh      # or tear the whole kind cluster down
```

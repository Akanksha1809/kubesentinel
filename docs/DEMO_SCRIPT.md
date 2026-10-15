# Demo script

A guided walkthrough for showing KubeSentinel off, matching what's implemented
as of Milestone 9 (observe/diagnose/persist/expose, policy-controlled
remediation, recovery verification, and observability -- see README.md's
status line for exactly what's left). Takes about 10 minutes for the full
walkthrough including remediation; stop after step 7 for the original
~5-minute diagnosis-only version.

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

**9. Be upfront about what this isn't, yet.**

Nothing in the live diagnosis pipeline *automatically* calls `propose()` --
every remediation above was a deliberate human action (or, in
`CONTROLLED_AUTO` mode, the policy engine's own explicit decision), never an
unattended reaction to a diagnosis. DNS/connectivity diagnosis is deliberately
out of scope (see DIAGNOSTIC_RULES.md), and there's no AI layer yet
(Milestone 11, optional). `TODO.md` lists exactly what's next and what's
known-incomplete; pull it up if asked "so what's missing."

## Cleanup

```bash
# Ctrl+C the demo.sh server
./lab/scripts/cleanup.sh     # removes scenario workloads, keeps the namespace/cluster
./lab/delete-cluster.sh      # or tear the whole kind cluster down
```

# Diagnostic rules

Every rule lives in `src/kubesentinel/diagnostics/rules/`, implements
`DiagnosticRule.evaluate(evidence: list[Evidence]) -> list[Hypothesis]`, and is a pure
function: no cluster access, no randomness, same input always produces the same
output. The engine (`diagnostics/engine.py`) runs every registered rule against the
incident's full evidence list and ranks the resulting hypotheses by confidence
(`HIGH > MEDIUM > LOW > INCONCLUSIVE`), with rule registration order as the tiebreak
for equal confidence.

## Implemented

**Milestone 1 (Pod-only evidence):**

### CrashLoopBackOffRule (`crash_loop.py`)

- **Trigger**: a container's `state.waiting.reason == "CrashLoopBackOff"`.
- **Confidence**: HIGH if `restart_count >= 3`, else MEDIUM.
- **Explicitly a symptom, not a root cause.** The explanation references whatever
  `lastState.terminated` reason/exit_code is available and says so in plain language:
  exit_code 0 suggests the process exits cleanly but something expects it to stay up;
  a non-zero code with no OOM signal suggests an application crash or
  misconfiguration that container logs would confirm; `OOMKilled` termination defers
  to the `OOMKilledRule` hypothesis as the more specific cause.
- **Missing evidence always includes** "container logs (not yet collected by this
  rule)" -- this rule doesn't read logs at all in Milestone 1, and says so rather than
  implying it did.

### OOMKilledRule (`oomkilled.py`)

- **Trigger (observed, HIGH confidence)**: `terminated.reason == "OOMKilled"` or
  `lastState.terminated.reason == "OOMKilled"` -- the kubelet's own explicit signal.
- **Trigger (inferred, MEDIUM confidence)**: `exit_code == 137` (SIGKILL) under the
  generic reason `"Error"`, with `restart_count >= 2`. This fallback exists because
  **this project's own kind-on-Colima/macOS dev cluster does not reliably set the
  OOMKilled reason**, confirmed live: the node's cgroup v2 `memory.events` showed a
  nonzero `oom_kill` counter while the Pod's container status reported generic
  `Error`/137. The inferred hypothesis's explanation says, in its own text, that it is
  inferred and names the alternative explanation (an external SIGKILL, e.g. node
  pressure eviction) it can't rule out. `missing_evidence` names exactly what would
  upgrade it to HIGH: the explicit reason, or a direct read of the node's
  `memory.events` for that container.
- A single `exit_code=137` with `restart_count < 2` does **not** fire -- not enough
  signal to distinguish from an unrelated one-off kill.

### ImagePullRule (`image_pull.py`)

- **Trigger**: `state.waiting.reason` in `{"ImagePullBackOff", "ErrImagePull"}`.
- **Confidence**: HIGH (both reasons are kubelet-reported facts, not inferred).
- Further classifies the waiting message into one of three buckets purely by string
  matching -- "not found"/"manifest unknown" -> likely nonexistent reference;
  "unauthorized"/"denied" -> likely missing/invalid registry credentials;
  "timeout"/"no such host"/"connection refused" -> likely network/registry
  connectivity. Falls back to "insufficient message detail" rather than guessing.

**Milestone 2 (new resource watches: Events, Deployments, PVCs, Nodes, EndpointSlices):**

### MissingConfigReferenceRule (`missing_config.py`)

- **Trigger**: `state.waiting.reason` in `{"CreateContainerConfigError", "CreateContainerError"}`.
- **Confidence**: HIGH (kubelet-reported fact, only set when a referenced ConfigMap/
  Secret or a specific key within one can't be resolved).
- The waiting message already names exactly what's missing (e.g. `configmap
  "app-config" not found`) -- surfaced verbatim, not re-parsed into a guess.

### PendingSchedulingRule (`scheduling.py`)

- **Trigger**: the Pod's own `PodScheduled` condition, `status=False`,
  `reason=Unschedulable` -- set by the scheduler itself, no Node watch needed.
- **Classification** of the scheduler's own message text into
  `PENDING_INSUFFICIENT_RESOURCES` (message contains "insufficient") or
  `PENDING_SCHEDULING_CONSTRAINT` (contains "taint", "node affinity"/"node
  selector"/"didn't match") -- HIGH confidence when a pattern matches.
- Falls back to `PENDING_SCHEDULING_CONSTRAINT` at MEDIUM confidence, with
  `missing_evidence` noting the message didn't match a known pattern, rather than
  guessing a specific cause it can't support.

### ReadinessProbeFailingRule / LivenessProbeFailingRule (`probes.py`)

- **Trigger**: a Warning Event with `reason=Unhealthy`, classified by message text
  containing "readiness probe failed" vs. "liveness probe failed".
- **Why these need the Event watcher specifically**: a failing readiness probe never
  touches `containerStatuses` at all -- the container keeps running, it's just
  removed from Service endpoints. There is no container-state signal to read; the
  Event is the *only* evidence. A failing liveness probe does eventually produce a
  restart (and, if sustained, `CrashLoopBackOffRule` will fire too), but the Event is
  what names the *actual* cause rather than leaving it as a generic crash.
- **Confidence**: HIGH (Kubernetes' own Event reason, not inferred).

### RolloutFailedRule / UnsatisfiedReplicasRule (`deployment.py`)

Deployment-kind incidents -- the first rules in this project that don't target a Pod.

- **RolloutFailedRule trigger**: `Progressing` condition `status=False`,
  `reason=ProgressDeadlineExceeded` -- HIGH confidence, set by the controller only
  after `progressDeadlineSeconds` elapses with no progress.
- **UnsatisfiedReplicasRule trigger**: `ready_replicas < desired_replicas` on the
  latest observation. Confidence depends on *why*:
  - HIGH if the `Progressing` condition's reason is `NewReplicaSetAvailable` (the
    controller itself considers the rollout done) -- a deficit after that means Pods
    are failing post-rollout, not that a rollout is still catching up.
  - HIGH if the `Available` condition is `False` -- confirmed live (see
    TODO.md's Known limitations #8): this condition, not `Progressing`, is what
    actually reflects post-rollout Pod failures in some real sequences.
  - Otherwise MEDIUM, with `missing_evidence` noting more observations over time
    would help -- a bare deficit is completely normal mid-rollout.
  - If MEDIUM, the rule also checks whether the deficit has been observed
    continuously for at least 30 seconds across multiple observations (using the
    real `observed_at` timestamps already on the accumulated evidence) and upgrades
    to HIGH if so.

### PvcBindingFailureRule (`pvc.py`)

- **Trigger**: a Warning Event with `reason` in `{"ProvisioningFailed",
  "FailedBinding"}` -- HIGH confidence.
- **Deliberately does NOT trigger on `status.phase == "Pending"` alone.** Confirmed
  against this project's own kind cluster: the default `local-path` StorageClass uses
  `volumeBindingMode: WaitForFirstConsumer`, under which a PVC is *correctly* Pending
  until a Pod references it. Treating that as a failure would fabricate a diagnosis.

### FailedMountRule (`mount.py`) -- added as part of Milestone 1's own "stabilize the core" audit, item 2

A different failure mode from `PvcBindingFailureRule`: a PVC can be genuinely
`Bound` (binding succeeded) while the kubelet still cannot mount the
underlying volume on the Node the Pod landed on.

- **Trigger**: a Warning Event with `reason == "FailedMount"` -- HIGH confidence.
  Evidence-source-agnostic: fires on any `FailedMount` Event regardless of
  which volume type or underlying cause produced it (an unreachable backing
  store, a multi-attach conflict, a wrong `fsType`, a missing ConfigMap/Secret
  reference, etc.) -- this rule names the symptom, not the specific cause.
- **Added `"FailedMount"` to `incidents/health.py::_BAD_EVENT_REASONS`** --
  without this, a Pod stuck FailedMount would never open an incident at all:
  the container never starts, so no waiting/terminated container state ever
  reflects the problem, and the Event is the *only* evidence this failure
  mode produces.
- **Also added `"FailedMount"` to `observer/service.py::_RELEVANT_EVENT_REASONS`**
  -- a *second*, independent Event-reason allowlist, upstream of the one
  above: it gates whether the real `EventWatcher` forwards the Event to
  `IncidentManager.observe()` at all. Missing this was a real bug caught only
  by live verification through the actual `ObserverService` -- the one-shot
  `diagnose` CLI never fetches Events, so it could never have surfaced this;
  without the fix, the Event was silently dropped before `_BAD_EVENT_REASONS`
  was ever consulted, and no incident opened despite the rule being correct.
- **Lab scenario** (`scenario-failed-mount`) uses a missing-ConfigMap volume
  reference, not the PVC-with-unreachable-storage case that originally
  motivated this rule -- confirmed live that this reliably, deterministically
  produces a real `FailedMount` Event with no external storage infrastructure
  needed, which this project's own kind-on-local-path lab doesn't have. See
  FAILURE_LAB.md for the honest accounting of what this scenario does and
  doesn't demonstrate.

### NodeConditionRule (`node.py`)

Node-kind incidents, cluster-scoped (needs `rbac/clusterrole.yaml`).

- **Trigger (HIGH)**: `Ready` condition `status != "True"`, or any of
  `MemoryPressure`/`DiskPressure`/`PIDPressure` conditions `status == "True"` --
  all direct kubelet-reported facts.
- **Trigger (MEDIUM)**: `spec.unschedulable == true` (cordoned). Capped at MEDIUM
  and does **not** by itself open an incident (see `has_failure_signal` in
  `incidents/health.py`) -- cordoning is routine, deliberate maintenance far more
  often than it's a problem. It's still reported (enriching an incident already open
  for another reason) because it IS relevant context when something else is wrong.

### ServiceSelectorMismatchRule (`service.py`)

Service-kind incidents, correlated from EndpointSlice watches (see
ARCHITECTURE.md for how; see TROUBLESHOOTING.md for a real upstream client bug hit
while building this one).

- **Trigger**: the Service's EndpointSlice(s) show **zero** endpoints total --
  MEDIUM confidence (capped: equally consistent with "selector typo" and "no
  matching Pods exist yet, e.g. the Deployment hasn't been created").
- **Deliberately does NOT trigger on zero *ready* endpoints with some endpoints
  present.** That means Pods matched the selector but aren't passing readiness --
  a Pod-level problem (`ReadinessProbeFailingRule`'s territory), not a Service
  misconfiguration. Firing this rule in that case would misdiagnose the actual cause.

## Ranking example (verified live)

The `oom-demo` lab scenario produces **both** `CRASH_LOOP_BACKOFF` (HIGH, from the
waiting reason) and `OOM_KILLED` (MEDIUM, inferred from exit_code 137) hypotheses for
the same Pod. The engine ranks `CRASH_LOOP_BACKOFF` first because it's HIGH and
`OOM_KILLED` is only MEDIUM here -- the symptom that was *directly observed* outranks
the root cause that was only *inferred*. This is intentional: confidence reflects how
certain the evidence actually makes the claim, not how specific or interesting the
claim is. If the runtime had reported the explicit `OOMKilled` reason, `OOM_KILLED`
would be HIGH and would rank first (registration order in `engine.DEFAULT_RULES` puts
`OOMKilledRule` before `CrashLoopBackOffRule` as the tiebreak for equal confidence).

## Why several rules filter evidence down to "latest per field" before matching

`IncidentManager` accumulates every observation's evidence into one growing
list (`incident.evidence`), never superseding an older observation of the
same field when a newer one arrives -- deliberate, since some rules
genuinely need that full history (`UnsatisfiedReplicasRule`'s sustained-
deficit timing across repeated `status.replicas` snapshots, in
`deployment.py`, is the one that does). But a rule whose own question is
"is this CURRENTLY true" must not scan that entire history for "any
matching entry, ever" -- found live: an incident that genuinely transitioned
`CrashLoopBackOff -> ErrImagePull` (the Pod's image was fixed mid-crash-
loop, no other change) stayed misdiagnosed as `CrashLoopBackOff`, because
nothing told the rule that its own earlier match had been superseded by a
later, different observation of the exact same field.

Closed with `diagnostics/rules/base.py::latest_by_source_field(evidence,
kind)` -- one entry per distinct `source_field` of a given `EvidenceKind`,
whichever is latest in `evidence`'s own append order (always chronological).
`ImagePullRule`, `MissingConfigReferenceRule`, `OOMKilledRule`, and
`CrashLoopBackOffRule`'s own waiting-state selector (above) all call this
first now, before applying their own match condition -- each of their
triggers is keyed off `CONTAINER_STATE`/`CONTAINER_TERMINATION` evidence
whose `source_field` already identifies one specific container's one
specific field (`containerStatuses[app].state.waiting`,
`...lastState.terminated`, etc.), so "latest per `source_field`" is exactly
"the current value of that field." `POD_CONDITION`/`DEPLOYMENT_STATUS`/
`NODE_CONDITION`/`PVC_STATUS`/`ENDPOINTSLICE_STATE` evidence is unaffected
(every rule reading them already either reads only the latest entry itself,
per container, such as `ServiceSelectorMismatchRule`'s `snapshots[-1]`, or
(the `UnsatisfiedReplicasRule` case) deliberately needs the full history).
`EVENT` evidence is also untouched -- its `source_field` is a constant
(`"event"`, not a per-occurrence key), so distinct Events correctly keep
coexisting rather than collapsing to the most recent one.

Live-verified against the real cluster against the exact scenario found:
`CrashLoopBackOff -> ErrImagePull -> CrashLoopBackOff` (patching a real
Pod's image to a bad tag, then back) on the same, continuously-open
incident. Before this fix, the final observation would have stayed
diagnosed `ERR_IMAGE_PULL`; confirmed it now correctly reports
`CRASH_LOOP_BACKOFF` again, via the real `/incidents` endpoint.

**A narrower, bounded, self-correcting case deliberately not chased
further**: during the brief window where a container is caught mid-restart
(`state.terminated` with the generic reason `Error`, between exiting and
actually restarting), there is briefly no current `.state.waiting` entry
of *any* kind for that container at all -- if a watch event happens to land
in that exact window, a rule could still match the previous observation's
now-stale `.state.waiting` entry, since nothing newer with that same
`source_field` exists yet to supersede it. This resolves itself on the very
next observation once the container settles into its new waiting state
(which does supersede the stale entry via the matching `source_field`) --
confirmed in the live reproduction above, where this exact mid-restart
state occurred and self-corrected within one more observation. A batch-
level (group-by-`observed_at`-per-container) redesign could close this
narrower residual case too, at meaningfully higher risk to the shared
evidence model for a transient inconsistency bounded to a single
observation cycle -- not done here.

## Not yet implemented (deliberately deferred -- see TODO.md)

DNS/connectivity failures. The original spec qualifies this one ("when the available
evidence supports a diagnosis"), unlike the other 13 categories. Passive evidence
alone gives very weak DNS signal; shipping a rule anyway would mean assigning
confidence the evidence doesn't support. **An active probe exists in this project
now** (`network/probe.py`, Capability D -- see `docs/NETWORK_DIAGNOSTICS.md` §12), so
"no active probe is out of scope for this project" is no longer the reason this
engine has no DNS rule. The real reason, found while re-examining this after the
probe shipped: that probe is off by default, explicitly opted into per call,
rate-limited, and takes up to ~20s to run -- structurally incompatible with
`DiagnosticEngine`'s rules, which are all pure, synchronous functions over
already-fetched evidence, invoked automatically on every watch event. Giving this
engine a DNS rule that calls the probe would mean a fundamentally different,
slower, mutating category of rule alongside the other 13, not a small addition to
this one. See `docs/IMPLEMENTATION_STATUS.md`'s "DNS and connectivity problems" row
for the full reasoning.

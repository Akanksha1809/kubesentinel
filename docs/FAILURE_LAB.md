# Failure lab

All lab resources live in the `kubesentinel-lab` namespace
(`lab/manifests/namespace.yaml`), never in a system namespace, and KubeSentinel's RBAC
(`rbac/role.yaml`) is scoped to that namespace only -- with one narrow, documented
exception for the network troubleshooting engine's CoreDNS/`kube-dns` health check
(`rbac/network-dns-role.yaml`, bound in `kube-system`; see RBAC_AND_THREAT_MODEL.md).

```bash
./lab/create-cluster.sh                      # create the kind cluster + namespace
./lab/scripts/run-scenario.sh crashloop       # apply one scenario
./lab/scripts/run-scenario.sh oom
./lab/scripts/run-scenario.sh imagepull
kubectl apply -f lab/manifests/scenario-transient-recovery/pod.yaml
./lab/scripts/run-scenario.sh oversized-resources   # resource optimization engine's
./lab/scripts/run-scenario.sh missing-requests      # own scenarios (not failures)
./lab/install-metrics-server.sh                     # optional, for CPU/memory recommendations
./lab/scripts/run-scenario.sh invalid-node-selector # scheduling advisor's own scenario
./lab/scripts/run-scenario.sh service-selector-mismatch  # network engine's own scenarios
./lab/scripts/run-scenario.sh service-port-mismatch
./lab/scripts/run-scenario.sh hpa-capacity-pressure # capacity planning advisor's own scenario
./lab/scripts/cleanup.sh                      # removes all scenario Deployments/Pods
./lab/delete-cluster.sh                       # destroys the kind cluster entirely
```

## Scenario: `crashloop` (CrashLoopBackOff)

- **Manifest**: `lab/manifests/scenario-crashloop/deployment.yaml`
- **How it fails**: the container runs `sleep 2; exit 1` on every start.
- **Expected Kubernetes behavior**: `restart_count` climbs; `state.waiting.reason`
  becomes `CrashLoopBackOff` after the first couple of restarts; `lastState.terminated`
  shows `reason=Error exit_code=1`.
- **Expected diagnosis**: `CRASH_LOOP_BACKOFF`, HIGH confidence once restarts >= 3.
  Explanation references the `Error`/exit_code=1 termination and says container logs
  are needed to confirm the application-level cause.
- **Automatic remediation**: not safe -- a restart cannot fix a deterministic exit.
- **Verified live**: incident opened, correctly diagnosed, visible via API/CLI/metrics.

## Scenario: `oom` (OOMKilled)

- **Manifest**: `lab/manifests/scenario-oom/deployment.yaml`
- **How it fails**: the container doubles an in-memory string in a loop
  (`x='a'; while true; do x="$x$x"; done`) inside a 16Mi memory limit, which the
  kernel's cgroup OOM killer enforces within a few seconds. The "unbounded" growth is
  bounded in practice by the container's own cgroup -- node memory is never at risk.
- **Expected Kubernetes behavior**: the container is killed on hitting the limit.
- **Observed quirk, confirmed on this project's own kind-on-Colima/macOS cluster**:
  the kubelet reported `reason=Error exit_code=137` instead of `reason=OOMKilled`,
  even though the node's own `cat /sys/fs/cgroup/memory.events` showed a nonzero
  `oom_kill` counter -- confirming the kill genuinely happened. See
  TROUBLESHOOTING.md and DIAGNOSTIC_RULES.md's OOMKilledRule section for how the rule
  handles this with an explicitly lower-confidence, labeled-as-inferred hypothesis.
- **Expected diagnosis**: `CRASH_LOOP_BACKOFF` (HIGH, from the waiting reason) ranked
  above `OOM_KILLED` (MEDIUM, inferred) in this environment; would rank `OOM_KILLED`
  (HIGH) first if the runtime reported the explicit reason.
- **Automatic remediation**: not safe -- restarting doesn't fix a memory limit that's
  genuinely too low; only a recommendation to raise it is appropriate.
- **Verified live**: both hypotheses present, correctly ranked, visible via API.

## Scenario: `imagepull` (ImagePullBackOff / ErrImagePull)

- **Manifest**: `lab/manifests/scenario-imagepull/deployment.yaml`
- **How it fails**: references `kubesentinel-lab-nonexistent/does-not-exist:v1`, a
  repository that doesn't exist on Docker Hub.
- **Expected Kubernetes behavior**: `state.waiting.reason` cycles `ErrImagePull` ->
  `ImagePullBackOff`, with a message containing "repository does not exist" /
  "insufficient_scope" style text from the registry.
- **Expected diagnosis**: `ERR_IMAGE_PULL` or `IMAGE_PULL_BACKOFF`, HIGH confidence,
  explanation classifies the message as a likely nonexistent-reference case.
- **Automatic remediation**: not safe -- only a human can supply the correct image
  reference; the rule recommends checking the reference and `imagePullSecrets`.
- **Verified live**: incident opened and correctly diagnosed as `ERR_IMAGE_PULL` on
  first observation, matching the registry's initial response before it moved to
  `ImagePullBackOff`.

## Scenario: `transient-recovery` (self-resolving CrashLoopBackOff)

- **Manifest**: `lab/manifests/scenario-transient-recovery/pod.yaml`
- **Why this one is a bare Pod, not a Deployment**: the point is to prove recovery of
  a *single Pod in place* (same UID throughout its restarts), which is exactly the
  resolution path `IncidentManager` implements today. A Deployment rollout fix
  replaces Pods (new UID), which does *not* currently auto-resolve the old incident --
  see TODO.md's "Known limitations".
- **How it fails, then recovers**: the container counts its own restarts in a file on
  an `emptyDir` volume (which survives container restarts within the same Pod,
  unlike the container's root filesystem). It exits 1 on its first two starts
  (simulating a dependency that wasn't ready yet), then runs `sleep infinity` from the
  third start onward.
- **Expected Kubernetes behavior**: `restart_count` reaches 2, `state.waiting.reason`
  becomes `CrashLoopBackOff`, then the container starts cleanly and stays
  Running/Ready with the same Pod UID throughout.
- **Expected KubeSentinel behavior**: opens a `CRASH_LOOP_BACKOFF` incident on first
  unhealthy observation, then marks it `RESOLVED` with a recorded
  `recovery_duration_seconds` once it observes the same Pod UID running healthily.
- **Verified live**: incident opened, then resolved with `recovery_duration_seconds`
  confirmed via `GET /incidents` -- the full observe-diagnose-recover loop, against a
  real cluster, not a mock.

## Scenario: `pvc-binding` (PersistentVolumeClaim binding failure)

- **Manifest**: `lab/manifests/scenario-pvc-binding/pvc.yaml`
- **How it fails**: references `storageClassName:
  kubesentinel-lab-nonexistent-storage-class`, which doesn't exist.
- **Expected Kubernetes behavior**: `status.phase` stays `Pending`, and the
  `persistentvolume-controller` emits a Warning Event with
  `reason=ProvisioningFailed` naming the missing StorageClass.
- **Expected diagnosis**: `PVC_BINDING_FAILURE`, HIGH confidence, from the Event --
  explicitly **not** from the `Pending` phase alone. This project's kind cluster's
  default StorageClass (`local-path`) uses `volumeBindingMode:
  WaitForFirstConsumer`, under which a PVC with no consuming Pod yet is *correctly*
  `Pending` -- see DIAGNOSTIC_RULES.md for why this distinction matters.
- **Automatic remediation**: not applicable -- only a human can supply a valid
  StorageClass name.
- **Verified live**: incident opened and correctly diagnosed via the exact manifest
  file above.

## Scenario: `service-selector-mismatch`

- **Manifest**: `lab/manifests/scenario-service-selector-mismatch/service.yaml`
- **How it fails**: the Service's selector (`app:
  this-label-matches-no-pod-in-the-namespace`) matches no Pod in the namespace.
- **Expected Kubernetes behavior**: the Service itself is created successfully
  (Kubernetes never validates that a selector matches anything) but its
  EndpointSlice has zero endpoints:
  `kubectl -n kubesentinel-lab get endpointslices -l kubernetes.io/service-name=service-selector-mismatch-demo`.
- **Expected diagnosis**: `SERVICE_SELECTOR_MISMATCH`, MEDIUM confidence -- capped
  deliberately, since zero endpoints is equally consistent with "selector typo" and
  "no matching Pods exist yet." See DIAGNOSTIC_RULES.md.
- **Automatic remediation**: not applicable -- only a human can fix the selector or
  confirm the intended Pods exist.
- **Verified live**: incident opened and correctly diagnosed via the exact manifest
  file above. Building the `EndpointSliceWatcher` this scenario depends on surfaced
  a real upstream `kubernetes` client bug -- see TROUBLESHOOTING.md.

## Scenario: `service-port-mismatch` (Milestone 4 of the platform spec --
## network troubleshooting engine)

- **Manifest**: `lab/manifests/scenario-service-port-mismatch/` (`deployment.yaml` +
  `service.yaml`).
- **How it fails**: unlike `service-selector-mismatch` above, this Service's
  selector genuinely matches a real, Ready Pod (`port-mismatch-demo`, which
  declares a container port named `http`) -- the break is the Service's
  `targetPort: web`, naming a container port the Pod never declares. A
  *named* `targetPort` has no numeric fallback: Kubernetes requires it to
  resolve to a real, declared `containerPort.name`, or that Service port has
  no usable endpoint on that Pod at all.
- **Expected Kubernetes behavior, confirmed live against this project's own
  kind cluster** (an assumption corrected during this milestone -- see
  IMPLEMENTATION_STATUS.md Capability D): the EndpointSlice controller
  *does* add a `ready: true` address for the Pod (the selector match is
  real), but with `ports: null`, since it still cannot resolve the
  unresolvable named `targetPort` to an actual port number:
  `kubectl -n kubesentinel-lab get endpointslices -l kubernetes.io/service-name=port-mismatch-demo -o yaml`.
- **Expected diagnosis**: `CONFIGURATION_ERROR`, from the independently-computed
  port match alone (named `targetPort` "web" not found among declared
  `containerPort` names on the matched Pod) -- not from the
  EndpointSlice-vs-selector consistency check, which stays consistent here
  since the ready-endpoint *count* still agrees even though its ports list is
  empty. See `docs/NETWORK_DIAGNOSTICS.md` Section 10.
- **Automatic remediation**: not applicable -- only a human can fix the
  Service's `targetPort` or the container's declared port name.
- **Verified live**: `kubesentinel network-check kubesentinel-lab
  <port-mismatch-demo pod>` correctly returned `CONFIGURATION_ERROR` with the
  named-targetPort mismatch named explicitly in the reasoning, confirmed via
  both the CLI and a real HTTP server (`POST /network/analyze`,
  `GET /network/findings[/{id}]`). The owning Deployment was correctly
  resolved. A separate check against the unrelated `remediation-test` Pod
  (selected by no Service at all) correctly returned `INCONCLUSIVE`, and the
  DNS health check correctly read the real `coredns`/`kube-dns` state in
  `kube-system` through the engine's new, narrowly-scoped RBAC grant.

## Scenario: `hpa-capacity-pressure` (Milestone 5 of the platform spec --
## capacity planning & autoscaling advisor)

- **Manifest**: `lab/manifests/scenario-hpa-capacity-pressure/` (`deployment.yaml` +
  `hpa.yaml`).
- **How it's built**: a real `HorizontalPodAutoscaler` pinned at
  `minReplicas: 1, maxReplicas: 1` (so it's permanently "at max" -- there's
  nowhere to scale to) targeting `cpu` Utilization at a deliberately
  absurd `1%` of request, paired with a Deployment whose one container
  runs a tight CPU busy-loop (`while true; do :; done`) so real usage
  reliably exceeds that 1% target the moment metrics-server reports its
  first sample -- no synthetic load generation beyond the busy-loop
  itself needed. The CPU request (1700m) is deliberately large relative
  to this lab's single kind Node (4 CPU allocatable).
- **Expected Kubernetes behavior, confirmed live**: `kubectl get hpa
  hpa-capacity-pressure-demo -n kubesentinel-lab` shows `cpu:
  <50-60%>/1%`, `REPLICAS: 1`, and a real `ScalingLimited`
  (`True`/`TooManyReplicas`) condition once the HPA controller computes a
  desired replica count above `maxReplicas` and can't act on it.
- **Expected diagnosis**: `CAPACITY` category, `HPA_AT_MAX` always (from
  the real "at max and still over target" condition).
  `SCALING_WOULD_BE_UNSCHEDULABLE` additionally fires when this lab's
  other steady-state scenarios' own CPU requests leave the single Node
  with no room for a second 1700m replica -- **honestly
  environment-dependent**, not deterministic in every environment: with
  every other lab scenario cleaned up first, there would be enough
  headroom for the check to instead report room is available. See
  `docs/CAPACITY_PLANNING.md` §10/§11 and the scenario manifests' own
  comments.
- **Automatic remediation**: not applicable -- only a human can decide
  whether to raise `maxReplicas`, lower the target, or add Node capacity.
- **A build-time correction, caught before shipping**: the first version
  of this scenario used an idle (`sleep infinity`) container, reasoned to
  trivially exceed a 1% target. Live-checking showed metrics-server
  rounds real usage below roughly 1m CPU down to 0%, so an idle container
  never triggered the "over target" condition at all -- fixed by giving
  the container a real (if synthetic) CPU-bound workload instead. A
  second correction: editing an already-running copy of this Deployment's
  template (rather than deleting and recreating it) deadlocked a rolling
  update, since the new Pod couldn't schedule while the old one -- at
  1700m -- still held the resources; noted in `deployment.yaml`'s own
  comment for future maintainers.
- **Verified live**: `kubesentinel optimize --namespace kubesentinel-lab`
  followed by `kubesentinel recommendations --category CAPACITY` and
  `kubesentinel recommendation <id>` correctly showed `HPA_AT_MAX` and
  `SCALING_WOULD_BE_UNSCHEDULABLE` together, with the real current/target
  utilization and real Node allocation numbers named explicitly in the
  reasoning, confirmed via both the CLI and a real HTTP server (`POST
  /optimization/analyze`, `GET /optimization/recommendations?category=CAPACITY`).
  `GET /optimization/workloads` correctly exposed the live `HpaSnapshot`
  (replica bounds, metrics, `ScalingLimited` reason) with zero route
  changes needed.

## Remediation test fixture (not a failure scenario)

- **Manifest**: `lab/manifests/remediation-test/deployment.yaml`
- **Purpose**: a healthy, stable, 2-replica Deployment that exists only to
  exercise remediation actions (restart/rollback/scale, Milestone 4) safely.
  Deliberately *not* broken and *not* under `scenario-*` naming, so a remediation
  demo is never confused with a diagnosis demo. 2 replicas specifically so
  restarting it never trips the policy engine's single-replica availability
  guard (see REMEDIATION_SAFETY.md).
- **Used for**: the live-verified scale/restart/rollback demonstrations in
  REMEDIATION_SAFETY.md. Apply with `kubectl apply -f
  lab/manifests/remediation-test/deployment.yaml`; it isn't included in
  `./scripts/demo.sh`'s default scenario set since it has nothing to diagnose.

## Scenario: `pending-resources` (Pending due to insufficient node resources)

- **Manifest**: `lab/manifests/scenario-pending-resources/deployment.yaml`
- **How it fails**: requests 100 CPU cores and 1000Gi memory -- deliberately
  absurd rather than "slightly too much," so this stays reliably unschedulable
  regardless of the host machine's own specs.
- **Expected Kubernetes behavior**: `status.conditions[PodScheduled]` stays
  `status=False, reason=Unschedulable`, with a message naming the specific
  shortage. Confirmed live:
  `message: "0/1 nodes are available: 1 Insufficient cpu, 1 Insufficient memory."`
- **Expected diagnosis**: `PENDING_INSUFFICIENT_RESOURCES`, HIGH confidence,
  classified directly from that scheduler message text (see
  `diagnostics/rules/scheduling.py::_classify`) -- not inferred from the
  `Pending` phase alone, which is also a Pod's normal, temporary resting state
  while waiting to be scheduled.
- **Automatic remediation**: not applicable -- only a human can decide whether
  to lower the request or add real node capacity.
- **Verified live**: incident opened and correctly diagnosed via the exact
  manifest file above, confirmed against the real `PodScheduled` condition.

## Scenario: `readiness-probe-failure`

- **Manifest**: `lab/manifests/scenario-readiness-probe-failure/deployment.yaml`
- **How it fails**: the container runs (`sleep infinity`, never crashes) but its
  `readinessProbe` is an `exec` command that always fails (`/bin/false`).
- **Expected Kubernetes behavior**: the Pod stays `Running` with `restartCount`
  at 0 -- nothing a crash-based rule would ever see -- but
  `containerStatuses[].ready` is `false` and the kubelet emits a recurring
  Warning Event: `reason=Unhealthy, message="Readiness probe failed: ..."`.
  Confirmed live.
- **Expected diagnosis**: `READINESS_PROBE_FAILING`, HIGH confidence, from that
  Event specifically. This is the one Pod-level rule in this project whose only
  evidence source is an Event, not `containerStatuses` -- see
  `diagnostics/rules/probes.py`'s module docstring. Also demonstrates why the
  one-shot `kubesentinel diagnose <ns> <pod>` CLI command (which only reads
  `pod_evidence`, no Events, by design -- it's a quick cluster lookup, not the
  full pipeline) reports this Pod as inconclusive, while the real
  observer+incident-manager pipeline (which does watch Events) diagnoses it
  correctly.
- **Automatic remediation**: NOT safe by default -- a restart does not fix a
  probe that is deterministically going to keep failing.
- **Verified live**: incident opened and correctly diagnosed via the exact
  manifest file above, through the real API server's observer pipeline.

## Scenario: `failed-rollout` (Deployment ProgressDeadlineExceeded)

- **Manifest**: `lab/manifests/scenario-failed-rollout/deployment.yaml`
- **How it fails**: the container crashes after 2s (same substrate as
  `crashloop`) *and* carries an always-failing `readinessProbe`.
  `progressDeadlineSeconds` is set to 15s (default 600s) so this fails fast
  enough for a live demo.
- **Found live while building this scenario**: the first version had no
  readinessProbe. Without one, a container is considered Ready the instant it
  enters `Running` -- so the crash-only Pod briefly satisfied the Deployment's
  "new ReplicaSet is available" check *before* it crashed, and the rollout got
  marked `Progressing=True/NewReplicaSetAvailable` instead of ever failing. The
  always-failing probe (same mechanism as `readiness-probe-failure` above)
  guarantees the Pod is never considered Ready even for that brief window, so
  the rollout genuinely cannot progress. See TODO.md's Known limitations for
  the full root cause.
- **Expected Kubernetes behavior**: after 15s with no progress, the Deployment
  controller sets `status.conditions[Progressing]` to
  `status=False, reason=ProgressDeadlineExceeded` -- confirmed live via
  `kubectl wait --for=jsonpath=...=ProgressDeadlineExceeded`.
- **Expected diagnosis**: `ROLLOUT_FAILED`, HIGH confidence, on the Deployment
  resource specifically -- a separate incident from the Pod-level
  `READINESS_PROBE_FAILING` (and, on an earlier ReplicaSet generation during
  manifest iteration, `CRASH_LOOP_BACKOFF`) incidents the same underlying Pods
  also produce, since the incident manager dedups by resource UID and a Pod and
  its owning Deployment are different resources. Only `ProgressDeadlineExceeded`
  triggers this -- a transient `Available=False` during a normal rollout does
  not (see REMEDIATION_SAFETY.md's "Why RESTART/ROLLBACK need a stricter
  check").
- **Automatic remediation**: NOT safe by default -- a restart will not fix a
  deterministically crashing/never-ready container; KubeSentinel recommends
  checking the new ReplicaSet's Pods directly, or rolling back if the previous
  revision is known good.
- **Verified live**: incident opened and correctly diagnosed via the exact
  manifest file above, confirmed against the real `Progressing` condition on
  the Deployment.

## Scenario: `oversized-resources` (resource optimization engine)

- **Manifest**: `lab/manifests/scenario-oversized-resources/deployment.yaml`
- **How it's set up**: the container only ever `sleep infinity`s -- real CPU
  usage reads as effectively 0m and real memory usage as a few hundred KB --
  while the Deployment requests 1000m CPU / 256Mi memory (limits 2000m/512Mi).
  This is not a *failure* scenario in the troubleshooting-engine sense: the
  Pod stays healthy and Ready the whole time.
- **Prerequisite for CPU/memory recommendations specifically**:
  `./lab/install-metrics-server.sh` (optional). Without it, `optimize` still
  runs, just with usage-based checks honestly staying silent.
- **Expected optimization behavior**: with metrics-server installed and at
  least 3 samples spanning at least 5 minutes (the default
  `LIMITED_HISTORY` threshold -- run `kubesentinel optimize` a few times,
  a couple of minutes apart), a `CPU_OVERSIZED` and a `MEMORY_OVERSIZED`
  recommendation both appear, proposing values near the configured floors
  (10m CPU / 16Mi memory by default). A single pass, or fewer than 3
  samples, correctly produces no proposed number at all.
- **No automatic action**: there is no apply/execute path for this engine;
  see RESOURCE_OPTIMIZATION_DESIGN.md §6.
- **Verified live** (TODO.md's Milestone 18 entry): ran a real long-lived
  API server through 4 `analyze()` calls spaced ~110s apart; by the 4th
  call (4 samples spanning 326s), both recommendations appeared with real
  observed-percentile numbers in `reasoning`, not placeholder text.

## Scenario: `missing-requests` (resource optimization engine)

- **Manifest**: `lab/manifests/scenario-missing-requests/deployment.yaml`
- **How it's set up**: the container has no `resources` block at all -- a
  common real misconfiguration, not a contrived edge case. The Pod stays
  healthy and Ready.
- **Expected optimization behavior**: `kubesentinel optimize` produces a
  `DATA_QUALITY` recommendation immediately (no samples needed, no
  metrics-server needed), naming exactly which of requests/limits are
  missing, with no proposed numbers -- there's nothing to compare usage
  against.
- **No automatic action**: same as above.
- **Verified live**: produced `DATA_QUALITY` from the very first
  `analyze()` call (`sample_count=1`), and was correctly superseded
  (marked `EXPIRED`) by each subsequent call's fresh row.

## Scenario: `invalid-node-selector` (scheduling advisor)

- **Manifest**: `lab/manifests/scenario-invalid-node-selector/deployment.yaml`
- **How it's set up**: `nodeSelector: {disktype: nonexistent-ssd-type}`, a
  label no Node in this cluster has -- a real, common misconfiguration (a
  typo'd or stale selector), not a resource-sizing problem. The container
  itself (`sleep infinity`) would run fine if ever scheduled; it never will
  be.
- **Expected Kubernetes behavior**: `status.conditions[PodScheduled]` stays
  `status=False, reason=Unschedulable`, with a message naming the selector
  mismatch (e.g. "didn't match Pod's node affinity/selector").
- **Expected troubleshooting-engine diagnosis**:
  `PENDING_SCHEDULING_CONSTRAINT` -- a different reason code from
  `pending-resources`'s `PENDING_INSUFFICIENT_RESOURCES`, since this is a
  selector mismatch, not a capacity shortfall.
- **Expected scheduling-advisor finding**: `kubesentinel schedule-check
  kubesentinel-lab <pod-name>` names the `NODE_SELECTOR` constraint
  specifically as the blocker (`disktype='nonexistent-ssd-type' (node has
  None)`), while `RESOURCE_FIT`/`TAINT_TOLERATION`/`REQUIRED_NODE_AFFINITY`
  all pass -- the point of this scenario existing separately from
  `pending-resources` is to demonstrate the advisor distinguishes *which*
  constraint blocked placement, not just that scheduling failed.
- **No automatic action**: this engine never patches a Pod's scheduling
  constraints; see docs/SCHEDULING_ANALYSIS.md Section 6.
- **Verified live**: `RESOURCE_FIT` correctly passed (real node allocatable
  4000m CPU / ~5.1Gi memory, ~2150m/~950Mi already requested by other lab
  Pods at analysis time -- genuine cluster numbers, not mocked) while
  `NODE_SELECTOR` correctly failed, confirmed both via the CLI and a live
  integration test (`tests/integration/test_scheduling_live.py`) that also
  confirmed zero non-GET HTTP calls during the full analysis pass.

## Image caching

All scenarios use `busybox:latest` with `imagePullPolicy: IfNotPresent` explicitly
set. See the root [README.md](../README.md#image-caching-note-for-kind-on-macoscolima)
for why this matters in a network-constrained dev environment and the exact commands
used to pre-load the image into the kind node when needed.

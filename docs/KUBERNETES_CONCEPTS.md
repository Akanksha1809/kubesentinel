# Kubernetes concepts, tied to this codebase

Every concept below is something this project actually exercises against a real
cluster, not a general Kubernetes primer -- each section names the exact code
that depends on the behavior described, and most were confirmed (or in several
cases, *corrected*) by running against a real kind cluster rather than assumed
from documentation. See TODO.md's Known limitations and TROUBLESHOOTING.md for
the live-caught surprises.

## Watches, resourceVersion, and the 410 Gone case

`observer/watchers.py::_ListWatchLoop` does LIST (to get a baseline + a
`resourceVersion`) then WATCH from that `resourceVersion`, instead of polling,
for every resource type this project watches (Pods, Events, Deployments, PVCs,
Nodes, EndpointSlices). The API server only keeps a bounded window of recent
changes in its watch cache; if a watch's `resourceVersion` falls outside that
window, the server returns HTTP 410 Gone rather than silently resuming from the
wrong point. The code treats 410 explicitly as "relist from scratch," not as a
generic error to retry blindly -- retrying a 410 with the same `resourceVersion`
would just get another 410. A periodic resync (full LIST, via
`timeout_seconds` on the watch stream itself, which the real API server honors
by ending the connection) also runs even on an otherwise-healthy watch stream,
as defense against any single missed event mattering --
`tests/integration/test_watch_resync.py` confirms this relist boundary actually
fires against a real API server, not just that the backoff math in
`observer/backoff.py` is correct.

## Events vs. authoritative resource state

A Kubernetes `Event` is *not* part of a resource's status; it's a separate
object the API server garbage-collects after a short TTL, and it only exists if
something happened to bother emitting one. `inspector.py::pod_evidence()`
reads the Pod's own `status.containerStatuses[].state` (the authoritative,
currently-true state) for crash/OOM/image-pull diagnosis, not Events -- the
authoritative-state-first principle is why `CrashLoopBackOffRule` still works
correctly even if the relevant Event already expired. But two diagnostic
categories genuinely have *no* authoritative-state signal at all and exist only
as Events: `ReadinessProbeFailingRule`/`LivenessProbeFailingRule` depend
entirely on the kubelet's `reason=Unhealthy` Event (see below) -- there is no
field on `PodStatus` that says "the readiness probe is failing right now."
Events also have their own GC lifecycle independent of the resource they
describe: `ObserverService._still_exists()` exists specifically because a
stale Event about an already-deleted PVC was found live to outlive the PVC
itself and would otherwise reopen a phantom incident (TODO.md #6).

Kubelet/client-go's event recording is also itself rate-limited -- a real,
separate behavior found live while building the Milestone 9 test suite: after
many hours of this project's own lab scenarios continuously re-triggering
identical `Unhealthy` events, a *freshly recreated* Pod's first probe-failure
Event took over a minute to actually appear in the API server, not the ~5s its
`periodSeconds`/`initialDelaySeconds` would suggest. This is client-go's
built-in event-spam filter (a per-process burst+rate-limit on repeated
identical reason/message events), not a KubeSentinel bug -- but it's a real
operational property of relying on Events for detection under sustained,
repeated identical failures.

## Why CrashLoopBackOff is a symptom, not a diagnosis

`CrashLoopBackOff` is the kubelet's own backoff behavior in response to a
container exiting repeatedly -- it says *that* the container keeps failing to
stay up, not *why*. `CrashLoopBackOffRule` encodes this directly in its
explanation text and in its `missing_evidence` list (always includes
"container logs," which this project never reads). The actual root cause could
be an OOM kill, a missing dependency, a bad config, or a deterministic
application bug -- which is why `OOMKilledRule` is a *separate* rule that can
fire alongside it, each citing its own evidence, rather than
`CrashLoopBackOffRule` trying to guess the underlying cause from the same
waiting reason.

## OOMKilled, exit code 137, and why they aren't quite the same signal

Exit code 137 is `128 + SIGKILL(9)` -- it means *some* process sent SIGKILL to
PID 1 in the container, which is consistent with, but not identical to, an OOM
kill (a node could also SIGKILL a container during eviction, or an operator
could `kill -9` it manually in rare setups). The kubelet's `reason=OOMKilled`
field is the more specific, authoritative signal *when the runtime sets it*;
this project confirmed live that not every kubelet/runtime/cgroup-driver
combination reliably does (see TROUBLESHOOTING.md). `OOMKilledRule` treats
these as two different confidence tiers rather than collapsing them, which is
the whole point of distinguishing observed facts from inferred hypotheses.

## Readiness vs. liveness probes: two different failure modes with the same Event reason

Both probe types report via the identical Event `reason=Unhealthy` -- the only
way to tell them apart is the message text ("Readiness probe failed: ..." vs.
"Liveness probe failed: ..."), which is exactly what
`diagnostics/rules/probes.py` does. The actual *consequences* are completely
different, and conflating them would misdiagnose the blast radius:

- A failing **readiness** probe never restarts the container and sets no
  waiting/terminated reason at all -- the container keeps running, it's just
  removed from Service Endpoints/EndpointSlices until the probe passes again.
  `restartCount` stays flat; only traffic routing is affected.
- A failing **liveness** probe causes the kubelet to kill and restart the
  container -- which, if it keeps failing, eventually *also* produces a
  `CrashLoopBackOff` waiting reason indistinguishable from a plain application
  crash unless the matching `Unhealthy` Event is read alongside it.

The `failed-rollout` lab scenario (`lab/manifests/scenario-failed-rollout/`)
depends on a third consequence found live while building it: a container with
*no* readiness probe at all is considered Ready the instant it enters
`Running`, with no check whatsoever -- so a plain crashing container can
briefly satisfy a Deployment rollout's "new ReplicaSet is available" check
*before* it crashes, preventing `ProgressDeadlineExceeded` from ever firing.
See FAILURE_LAB.md and TODO.md #19.

## The scheduler's own unschedulability message, not an inferred guess

A Pod the scheduler cannot place reports
`status.conditions[PodScheduled]={status: False, reason: Unschedulable}` with
a `message` the scheduler itself writes, naming the specific cause (e.g. "0/1
nodes are available: 1 Insufficient cpu, 1 Insufficient memory." or "... 1
node(s) had untolerated taint ..."). `diagnostics/rules/scheduling.py`
classifies HIGH confidence directly from that message rather than guessing from
the bare `Pending` phase, which by itself is also a Pod's completely normal,
temporary resting state while waiting to be scheduled -- treating `Pending`
alone as a failure would fabricate a diagnosis the evidence doesn't support.
This condition is written directly onto the Pod's own status (not routed
through the separate Event pipeline), so it's immediate and stable -- no
backoff, no event-rate-limiting, which is why `tests/integration/`'s
delete-and-recreate dedup test specifically uses this scenario rather than a
crash-based one (see TROUBLESHOOTING.md-style notes in
`tests/integration/conftest.py`).

## Deployment rollout semantics: why aggregate replica counts lie during a rollout

Kubernetes deliberately keeps a Deployment's *old* ReplicaSet's Pods serving
during a rolling update, so `readyReplicas`/`availableReplicas` can look fully
satisfied via Pods that are about to be replaced, well before the *new*
ReplicaSet has done anything. Found live, the hard way, twice:

1. **The recovery verifier** (Milestone 5) initially reported a restart
   `RECOVERED` within 0.1s, while `kubectl get pods` still showed the new Pod
   `ContainerCreating` and the old Pods `Running` -- fixed by additionally
   requiring `updatedReplicas >= desired` for `RESTART_DEPLOYMENT`/
   `ROLLBACK_DEPLOYMENT` specifically (not `SCALE_DEPLOYMENT`, which starts no
   new rollout at all). This is exactly what `kubectl rollout status` itself
   waits for.
2. **The same verifier, separately**, treated a bare
   `Available=False/MinimumReplicasUnavailable` condition as a definitive
   failure -- but that condition is *legitimately transient* during any normal
   scale-up or rollout (a few seconds while a newly-requested replica hasn't
   started yet), not evidence anything is actually wrong. Fixed with a
   stricter `_deployment_definitively_stuck()` check used only for the
   verifier's FAILED path, trusting only `ProgressDeadlineExceeded` -- a
   condition the controller sets *only after its own configured deadline has
   elapsed with zero progress*, which is the one truly durable "this rollout
   has given up" signal. `RolloutFailedRule` (the passive diagnostic rule,
   different from the active verifier above) uses the same
   `ProgressDeadlineExceeded` signal for the identical reason.

`progressDeadlineSeconds` itself defaults to 600s; the `failed-rollout` lab
scenario sets it to 15s purely to make a live demo fast -- the controller
behavior it exercises is identical either way, just on a shorter clock.

## PVC binding and `volumeBindingMode: WaitForFirstConsumer`

A `PersistentVolumeClaim` with no Pod yet consuming it is *correctly*
`Pending` under the `WaitForFirstConsumer` binding mode (this project's kind
cluster's default `local-path` StorageClass uses it) -- binding is deliberately
deferred until a Pod actually needs the volume, so the scheduler can place the
Pod and the volume together. `PvcBindingFailureRule` diagnoses
`PVC_BINDING_FAILURE` from the `persistentvolume-controller`'s own Warning
Event (`reason=ProvisioningFailed`, naming the actual problem -- e.g. a
nonexistent StorageClass), explicitly *not* from the bare `Pending` phase,
which would misdiagnose every normal `WaitForFirstConsumer` PVC in the cluster
as broken.

## Service/EndpointSlice selector mismatches, and a real upstream client bug

A Service's `spec.selector` matching zero Pods is not something Kubernetes
itself validates at creation time -- the Service is created successfully
regardless, and the only observable symptom is that its EndpointSlice has zero
endpoints. `ServiceSelectorMismatchRule` diagnoses this at MEDIUM confidence
(deliberately capped: zero endpoints is equally consistent with "selector typo"
and "no matching Pods exist yet," and this project doesn't guess which).
Building the `EndpointSliceWatcher` this depends on surfaced a real bug in the
generated `kubernetes` Python client: `V1EndpointSlice.endpoints` is marked a
required list field, but the API server legitimately sends JSON `null` for a
genuinely empty list (Go's `omitempty` serializing a nil slice), and the
client's strict deserializer raised and killed the *entire* list/watch call on
it -- see TROUBLESHOOTING.md for the `raw_object_watch` workaround.

## Node conditions and the one necessary cluster-scoped RBAC exception

`Node` is not a namespaced resource, so watching it (`NodeConditionRule`)
cannot be expressed in a namespaced `Role` no matter how narrow --
`rbac/clusterrole.yaml` is the one deliberate `ClusterRole` in this entire
project, read-only, scoped to exactly `nodes`/`get,list,watch`. See
RBAC_AND_THREAT_MODEL.md.

## RBAC verb semantics: PATCH and PUT are genuinely different verbs

`kubernetes.client`'s `patch_namespaced_deployment` issues an HTTP PATCH, which
RBAC maps to the `patch` verb; `replace_namespaced_deployment` issues a full
HTTP PUT, which RBAC maps to `update` -- a *different* verb that `patch` alone
does not satisfy. This was caught live (TODO.md #14): `rbac/remediation-
role.yaml` initially granted only `patch`, and every manual verification of
rollback up to that point had run under an admin kubeconfig, which would never
have surfaced the resulting 403 against the real `kubesentinel` ServiceAccount.
Confirmed with `kubectl auth can-i update deployments --as=...`: `no` before
adding the verb, `yes` after -- now also covered by an automated live test,
`tests/integration/test_rbac_boundary.py`.

A related, separate subtlety governs *why* rollback uses PUT at all rather than
a merge PATCH: a strategic merge-patch body's absent fields mean "no change,"
never "clear it." Rolling back to an earlier revision's template can require
*removing* a field the current Deployment has (e.g. a restart annotation) that
the target revision's template doesn't -- a merge PATCH cannot express that
removal, only a full `replace` (PUT) can. `RemediationExecutor._rollback()`
uses `replace_namespaced_deployment` for exactly this reason (TODO.md #12).

## Owner-reference resolution, and a real `omitempty` surprise in replica counts

Resolving "which controller owns this Pod" means walking
`metadata.ownerReferences`, preferring the entry with `controller: true`
(a Pod can, in principle, carry more than one owner reference) --
`optimization/ownership.py::resolve_pod_owner()` does this explicitly
rather than assuming the first entry is authoritative. For a Deployment
specifically, the chain is two hops, not one: a Pod's owner is its
ReplicaSet, and the ReplicaSet's own owner (looked up separately) is the
Deployment -- `optimization/inventory.py` builds a `{uid: ReplicaSet}` map
once per inventory pass specifically to walk this chain without an extra
API call per Pod.

Found live, confirmed against the raw API response rather than assumed: a
Deployment/ReplicaSet's `status.readyReplicas`/`status.numberReady` field
is entirely *absent* from the JSON the API server sends when its value is
`0`, not present with a value of `0` -- Go's `omitempty` tag on an `int`
field serializes the zero value as "field not there at all," and the
generated Python client deserializes that absence as `None`. Code that
naively checks `status.ready_replicas or 0` gets this right by accident;
code that treats `None` as "unknown" rather than normalizing it to `0`
would misreport a genuinely-broken Deployment (0 ready) as "ready count
unknown." `inventory.py::_replica_counts()` normalizes explicitly, with a
comment citing exactly this. `spec.replicas` has no such behavior (Go
`omitempty` only applies to the zero value of the field's type, and a
pointer/explicit-default field like `spec.replicas` is set by an admission
default, not left absent) -- the fix is deliberately scoped to the two
status fields that actually exhibit it, not applied defensively everywhere.

## The Metrics API is an aggregated API, and `kubectl auth can-i` doesn't
always know that

`metrics.k8s.io` isn't served by `kube-apiserver` itself -- it's a
separate `APIService` that aggregates requests out to the metrics-server
Deployment running as a regular Pod in the cluster. Two consequences
confirmed live while verifying `rbac/optimization-role.yaml`:

1. **It can be down independently of the control plane.** A `503
   Service Unavailable` from `/apis/metrics.k8s.io/v1beta1/...` can mean
   metrics-server itself is unreachable or crash-looping (confirmed via
   `kubectl describe pod -n kube-system -l k8s-app=metrics-server` showing
   probe timeouts under this project's own documented shared-VM
   contention) -- a real cluster-admin kubeconfig gets the exact same
   `503` in that state, which is how this was confirmed as "metrics-server
   is down," not "the kubesentinel ServiceAccount lacks permission."
2. **`kubectl auth can-i` can give a wrong answer for it.** Checking
   `kubectl auth can-i list pods.metrics.k8s.io --as=<SA>` answered `no`
   (with a warning that the resource type wasn't found in client-side API
   discovery) even *after* `rbac/optimization-role.yaml` was applied and
   genuinely granted it -- confirmed by minting a real token for that exact
   ServiceAccount and making a direct authenticated HTTP request, which
   returned a real `200` with real pod metrics. `can-i`'s own client-side
   discovery cache is the unreliable part here, not RBAC itself; the
   authoritative check for an aggregated API is a real request as the real
   identity, not the `can-i` convenience tool.

## Idempotency and concurrency: what this project actually guarantees

`RemediationExecutor` re-reads the live resource and verifies its UID still
matches what the policy decision was made against before mutating anything --
the resource may have changed (or been deleted) between diagnosis and
execution. Concurrency across *different* resources needs no special handling
(independent targets, independent repository rows); concurrency across
*multiple callers proposing against the same incident* does, and was found
live to be a real gap (TODO.md #20): `RemediationService.propose()`'s budget
check reads, decides, and persists in three separate steps, and without a lock
around that sequence, concurrent callers could all read "0 actions so far"
before any of them saved, bypassing the per-incident budget entirely. Fixed
with a single process-wide lock -- proportionate for this project's
single-process, local-tool design, not a distributed-systems-grade solution.

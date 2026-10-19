# Closing a stale Pod incident after a Deployment rollout

Design for the oldest open item in `TODO.md`'s "Known limitations" section
(Milestone 1): **"Pod-UID dedup doesn't survive a Deployment rollout."**
`IncidentManager.observe()` dedups by `resource.uid`
(`incidents/manager.py`); a rollout replaces the old Pod with a new one
under a new UID, so the old Pod's incident never receives another
observation at all -- nothing ever calls `observe()` for a UID that no
longer exists -- and it sits open forever, waiting for a human to notice
and close it by hand.

This document is a design only. Nothing here has been implemented.

## 1. What "fixed" has to mean, precisely

`_resolve()` today means one specific, narrow thing: *this exact object*,
by UID, was observed unhealthy and is now observed healthy, with a real
`recovery_duration_seconds` measured against its own `first_observed_at`.
That claim must stay true -- it is the basis for every recovery-time
metric and every `RESOLVED` row this project has ever produced. A Pod
replaced by a rollout is never going to report healthy again; there is no
later observation to confirm. So this is deliberately **not** "detect
that the old Pod recovered" -- it's "notice that the old Pod is gone, its
parent Deployment is healthy, and close the stale incident with an
honest label that says exactly that and nothing more."

**New terminal `IncidentStatus`: `SUPERSEDED`** (not a repurposing of the
already-reserved-but-unused `UNRESOLVED` value -- that name's original
intent is unknown and this needs its own precise meaning, not an
overload). A `SUPERSEDED` incident carries:
- `resolved_at`: when the sweep closed it (still useful for "how long did
  this sit open" bookkeeping).
- `recovery_duration_seconds`: left `None`, always -- there is no
  recovery to measure, and setting one would quietly corrupt every
  recovery-time metric/dashboard with a number that was never actually
  observed. `RESOLVED` and `SUPERSEDED` must stay distinguishable in
  every consumer (`/metrics`, the CLI's `incidents --status` filter, any
  future dashboard) precisely so no one has to guess which numbers are
  real.
- An audit entry naming the replacement: which Deployment resolution
  triggered the sweep, and that the Pod was confirmed gone via a live
  check, not inferred.

## 2. The ownership-chain problem, and why it rules out the easy wrong answer

A Pod's `ResourceRef.owner_kind`/`owner_name`/`owner_uid`
(`observer/inspector.py::resource_ref_from_pod`) is **one level** of
ownership: for a Deployment-managed Pod, that's its ReplicaSet, not the
Deployment. A rollout creates a *new* ReplicaSet (the old one is usually
kept around, scaled to 0, for rollback -- until `revisionHistoryLimit`
prunes it), so the Pod incident's own `owner_uid` is a ReplicaSet UID
that itself may or may not still exist, and never directly names the
Deployment at all.

The wrong answer is matching by name prefix (stripping the
`-<replicaset-hash>-<pod-hash>` suffix to guess the Deployment's name) --
fragile string heuristics for a correctness-sensitive decision are
exactly what this project has refused to do everywhere else (RBAC
built-in-subject exclusion by prefix is the one deliberate exception, and
even that is an allowlist narrowing a safe default, not an identity
claim). The real answer already exists in this codebase, built for
exactly this problem in a different capability:
`optimization/ownership.py::resolve_pod_owner()` walks Pod -> ReplicaSet
-> Deployment using each object's own, authoritative
`ownerReferences` -- bulk-fetching every ReplicaSet in the namespace once
(avoiding an N+1 API call pattern) -- and returns `None`, conservatively,
whenever the chain can't be verified (the ReplicaSet was already pruned,
an unrecognized controller, a genuine race between list calls). This
design reuses that same function, or the same algorithm against a
narrower live read, rather than inventing a second ownership resolver.

**The chain determines eligibility, not identity-by-guessing.** For a
candidate stale Pod incident to even be considered: its own recorded
`owner_uid` (the ReplicaSet at the time it was observed) must resolve,
*live, right now*, to a Deployment -- by that Deployment's own `uid`,
never by name -- that matches the Deployment whose health check just
triggered the sweep. If the old ReplicaSet has already been pruned, the
chain can't be verified, and per `resolve_pod_owner`'s own existing
contract the incident is left alone, open, exactly as today. This is a
real, named limitation of this design (see §6), not something to paper
over with a guess.

## 3. The trigger: every healthy Deployment observation, not just a resolving one

The obvious-looking hook -- "when a Deployment's *own* incident
resolves, sweep its Pods" -- misses the single most common real case: a
Deployment that rolls out cleanly on the first try never opens its own
incident at all (no `ROLLOUT_FAILED`/`UNSATISFIED_REPLICAS` hypothesis
ever fires), yet the old Pod's incident is exactly as stale as if the
rollout had struggled first. Gating on "an incident just resolved" would
silently skip the ordinary, successful case this fix most needs to
cover.

Instead: **every time `IncidentManager.observe()` is called for a
Deployment resource and that observation's own evidence satisfies
`is_confirmed_healthy()`** (the exact same check `observe()` already
uses to resolve the Deployment's own incident, reused unchanged -- no
new health definition), check whether there's anything to sweep at all:

```python
candidates = [
    i for i in self._repo.list(namespace=resource.namespace, status=None)
    if i.resource.kind == "Pod"
    and i.resource.owner_kind == "ReplicaSet"
    and i.status not in (IncidentStatus.RESOLVED, IncidentStatus.SUPERSEDED)
]
```

A cheap, local, no-API-call repository query. If `candidates` is empty
(the common case once the fix has been running a while -- most
namespaces have no stale incidents sitting around), nothing further
happens: no ReplicaSet list call, no per-Pod existence check, every
healthy Deployment observation stays as fast as it is today. This
reuses the **existing** `DeploymentWatcher`'s own cadence (fires on
every real Deployment change, and again on every resync interval) --
no new scheduler, no periodic sweep process, consistent with this
project having exactly zero cron-like background jobs anywhere today.

## 4. The sweep itself

Only runs when step 3 found at least one candidate:

1. Fetch every ReplicaSet in the namespace live
   (`apps.list_namespaced_replica_set`, unfiltered -- the same call
   `optimization/inventory.py` already makes once per inventory pass,
   correlated afterward by UID rather than by a label-selector query;
   not reinvented here).
2. For each candidate incident: look up its recorded `owner_uid` in that
   ReplicaSet list by UID, and confirm *that* ReplicaSet's own
   `ownerReferences` names this exact Deployment (by `uid`) --
   `resolve_pod_owner()`'s own exact algorithm, reused rather than
   duplicated. If the UID isn't in the live list at all, or its
   ownerReferences point somewhere else, skip this candidate -- leave it
   open (§2, §6).
3. For a candidate that passes step 2: confirm the Pod itself is
   actually gone -- `core.read_namespaced_pod(name, namespace)` and
   expect a 404 (`ApiException.status == 404`). A Pod that still exists
   (even if its UID now differs -- a name reused by something else
   entirely) is **not** touched by this sweep at all; that is a
   genuinely different situation (a new, distinct object happens to
   share a name) that this design does not attempt to classify, the same
   restraint `_still_exists()` in `observer/service.py` already applies
   to a different "is this the same object" question.
4. Only once both checks pass: call a new
   `IncidentManager._supersede(incident, now, replaced_by_deployment=resource.name)` --
   mirrors `_resolve()`'s own shape exactly (sets `status`,
   `resolved_at`, an audit entry), decrements `incidents_active` the same
   way `_resolve()` already does, but never touches
   `recovery_duration_seconds` or the `recovery_duration_seconds`
   histogram (§1).

Every step above is a live, verified fact (a 404, a matching
`ownerReferences` entry) -- never an inferred "probably fine." This
mirrors the design standard every other milestone in this project has
already been held to.

## 5. Where this lives, and what it deliberately does not touch

Entirely inside `IncidentManager` -- a new private method called from
`observe()` right after the existing Deployment-resolution branch, the
same shape `_enrich_with_recommendations()` already uses for its own
"after diagnosis, do one more thing" step. `DiagnosticEngine` stays a
pure function of evidence with zero repository or live-cluster access --
this is a service-layer concern querying the repository and the cluster
directly, same hard architectural line every other service-layer
addition in this project has respected.

Nothing about `_resolve()`'s own existing behavior changes. Nothing about
how a *new* incident opens changes. The API/CLI need exactly one
addition: `SUPERSEDED` as a valid value wherever `RESOLVED` already is
(the `status` query param on `GET /incidents`, the CLI's `--status`
filter) -- no new route, no new command.

## 6. What this design does not attempt

- **Does not fix a pruned-ReplicaSet gap.** If `revisionHistoryLimit`
  (Kubernetes default: 10) prunes the old ReplicaSet before this sweep
  ever runs against it, the chain in §2/§4 step 2 can't be verified and
  the incident is left open, exactly as it is today. Narrower than the
  current gap (most single rollouts keep their immediately-prior
  ReplicaSet around), not eliminated.
- **Does not supersede a standalone Pod's incident** (no owner at all)
  or one owned by a StatefulSet/Job -- this design is scoped specifically
  to the Deployment -> ReplicaSet -> Pod chain named in the original
  limitation. **DaemonSet closed as a later, separate follow-up -- see
  §8.** StatefulSet and Job remain open, and for a reason found only while
  closing DaemonSet, not assumed going in: a StatefulSet Pod's own name is
  *reused* across recreation (ordinal identity), so the "confirm via a
  live 404 on the old recorded name" check this design and §8 both rely
  on can't tell "replaced" from "this is still the same Pod" for that
  kind -- a materially different problem, not a mechanical copy of this
  one. A Job generally has no stable per-attempt identity to match an old
  incident to its replacement by at all (only `completionMode: Indexed`
  Jobs would -- via the completion-index label -- and most Jobs don't use
  it).
- **Does not retroactively close incidents that predate this feature**
  on upgrade -- the sweep only runs on a live Deployment observation
  going forward; an already-ancient stale incident from before this
  shipped stays open until its own Deployment next reports healthy
  (which, for an actually-healthy Deployment, should be the very next
  watch cycle after this ships -- but is not instantaneous or guaranteed
  for a Deployment that's been deleted entirely in the meantime, which
  this design also does not attempt to detect).
- **Does not change `recovery_duration_seconds` semantics or backfill
  one.** §1 is the load-bearing constraint of this whole design.

## 7. Implemented and live-verified

This design has been implemented (`IncidentStatus.SUPERSEDED`;
`IncidentManager._maybe_supersede_stale_pod_incidents`/`_supersede`; the
sweep trigger in `observe()`; `optimization/ownership.py::controller_ref`
promoted to public and reused, not duplicated; `clients` wired into
`IncidentManager` in `api/app.py`'s lifespan and
`evaluation/harness.py`), with 10 new unit tests (all passing on their
first run -- no bug was caught by the tests themselves this time).

Live-verified against the real cluster with a throwaway crash-looping
Deployment (not a permanent lab scenario, since every permanent scenario
in `lab/manifests/` is relied on by some other milestone's live test to
*stay* broken): a real Pod-level `CRASH_LOOP_BACKOFF` incident opened,
the Deployment was patched to a command that actually succeeds (a real
rollout: new ReplicaSet, new Pod, old Pod terminated), and once the
Deployment reported healthy the old Pod's incident was confirmed,
precisely:

```
status: SUPERSEDED
resolved_at: 2026-10-17T09:55:23.652394Z
recovery_duration_seconds: None
last audit entry: {'event_type': 'incident_superseded', 'actor': 'observer',
  'details': {'replaced_by_deployment': 'supersede-test-demo',
    'reason': 'pod confirmed gone (live 404); owning Deployment observed healthy'}}
```

The Deployment's own incident resolved through the unchanged, ordinary
`RESOLVED` path alongside it (`recovery_duration_seconds: 221.4`, a real
measured value) -- confirming the new sweep didn't disturb the existing
resolution path it runs next to.

Two environmental complications were hit along the way, both self-
resolved and neither a code defect: the host's chronic shared-VM
contention (documented repeatedly throughout this project's history)
showed up twice during this one verification run -- once as
`kube-controller-manager` stuck retrying with `"too many open files"`
for several minutes before a ReplicaSet could even be created, and again
as a second `kube-controller-manager`/`kube-scheduler` `CrashLoopBackOff`
episode that delayed the rollout itself. Both were waited out, not
worked around. Separately: this verification also caught and cleaned up
several *stale* throwaway demo-server processes left running from
earlier, unrelated points in this same session -- an operational slip in
how this agent cleaned up after itself, not a finding about the feature
under test.

## 8. DaemonSet, closed as a later, separate follow-up

Closes §6's own named gap, for exactly the one kind it's tractable for
without a larger redesign: a DaemonSet-owned Pod's incident now gets
superseded too, once a healthy replacement Pod is observed on the same
node.

### 8.1 Why the trigger has to be different, not just the matching

The Deployment sweep fires off the *owning controller's own* confirmed-
healthy observation (§3) -- this project watches Deployments directly,
with their own `DEPLOYMENT_STATUS` evidence and `has_failure_signal`/
`is_confirmed_healthy` checks. There is no equivalent watcher for
DaemonSet, and adding one (plus the evidence/diagnosis machinery to
decide "is this DaemonSet healthy") would be a materially larger
undertaking than this gap warrants on its own.

Used instead: a single DaemonSet-owned Pod's own confirmed-healthy
observation -- already produced by the existing `PodWatcher`/
`is_confirmed_healthy`, no new watcher needed. This is a weaker signal
than "the whole controller is healthy" (it says nothing about any other
node), which is exactly why the matching below is scoped to the one
node that signal actually speaks for, not to "every stale incident for
this DaemonSet."

### 8.2 Why the match is (DaemonSet, node), not (DaemonSet, name) like the Deployment case

The Deployment sweep confirms supersession with a live 404 on the *old
incident's own recorded Pod name* -- safe there because a Deployment's
replacement Pod always gets a fresh, generated name (never reused). A
DaemonSet-owned Pod's name is *also* freshly generated on every
recreation (unlike a StatefulSet's ordinal name), so the same 404 check
is valid here too -- but only once the right *candidate* has been found,
and a Deployment-style "same ReplicaSet chain" filter doesn't exist for
a DaemonSet (no intermediate controller at all:
`resolve_pod_owner`/`_DIRECT_OWNER_KINDS` already resolves a DaemonSet-
owned Pod's `owner_uid` straight from its own single ownerReference,
confirmed by reading `resource_ref_from_pod` -- no live ReplicaSet-style
lookup needed, unlike §4 step 2).

`ResourceRef` gained `node_name` (from `pod.spec.node_name`, populated by
`resource_ref_from_pod` for every Pod, set once scheduled) specifically
to serve as this kind's "slot" identifier -- the role a Deployment's
ReplicaSet chain plays, but simpler: no live API call is needed to
establish it, since it is already recorded on the incident at open time.
A candidate must match the triggering Pod's own `owner_uid` *and*
`node_name`, and must not be the triggering Pod's own `resource_uid`
(excluded explicitly, not merely assumed impossible) -- a genuinely
still-unhealthy Pod on a *different* node must never be superseded just
because *some other* node's Pod came back healthy; this is the load-
bearing safety property a single Pod's own health, instead of a
controller's aggregate health, makes necessary.

### 8.3 What was added

- `models/incident.py::ResourceRef.node_name: str | None` -- additive,
  only ever set for a Pod.
- `observer/inspector.py::resource_ref_from_pod` -- populates it from
  `pod.spec.node_name`.
- `incidents/manager.py::IncidentManager._maybe_supersede_stale_daemonset_pod_incidents`
  -- the DaemonSet-scoped sweep described above, triggered from `observe()`
  whenever `resource.kind == "Pod" and resource.owner_kind == "DaemonSet"
  and is_confirmed_healthy(evidence)`, mirroring the Deployment trigger's
  own placement and reasoning (§3) exactly.
- `IncidentManager._supersede` generalized from a Deployment-specific
  `replaced_by_deployment` parameter to a kind-agnostic `replaced_by` +
  explicit `reason`, shared by both sweeps now rather than duplicated.

### 8.4 Live-verified against the real cluster

A real single-node `kind-kubesentinel` cluster (no multi-node setup
needed -- a DaemonSet's one Pod and its replacement both land on the
cluster's only node by construction): created a crash-looping DaemonSet,
confirmed a real `CRASH_LOOP_BACKOFF` Pod-level incident opened, then
patched the DaemonSet's own template to a healthy command -- a real
rolling update, old Pod deleted, new Pod created with a fresh name on
the same node. Once the new Pod reported healthy, the old incident
correctly showed:

```
status: SUPERSEDED
resolved_at: 2026-10-19T17:04:54.749020Z
recovery_duration_seconds: None
last audit entry: {'event_type': 'incident_superseded', 'actor': 'observer',
  'details': {'replaced_by': 'daemonset-supersede-demo',
    'reason': 'pod confirmed gone (live 404); a healthy replacement pod observed on the same node'}}
```

-- `recovery_duration_seconds` correctly `None` (§1's constraint holds
here too), `incidents_active{category="CRASH_LOOP_BACKOFF"}` correctly
back to `0` via the real `/metrics` endpoint. Cleaned up the demo
DaemonSet afterward; namespace confirmed empty.

StatefulSet and Job remain open, named explicitly in §6, for the reasons
found while scoping this closure, not assumed going in.

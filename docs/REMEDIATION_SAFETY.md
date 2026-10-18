# Remediation safety

Status: **policy engine (Milestone 3), the remediation executor + service
(Milestone 4), and the recovery verifier (Milestone 5) are all implemented, tested,
and verified live.** This document describes the code as it actually behaves.

## The policy engine (`policy/engine.py::PolicyEngine`)

Takes a `PolicyRequest` (proposed action type + target, the triggering incident's
severity and hypothesis confidence, whether the diagnosing hypothesis itself permits
auto-remediation, "now," and the target's recent action history) and returns a
`PolicyDecision`: one of `ALLOWED`, `REQUIRES_APPROVAL`, or `DENIED`, always with a
human-readable `reason`. It is a pure function of its inputs -- no Kubernetes API
calls, no database queries, no clock reads of its own. The caller
(`remediation/service.py::RemediationService`, or a test) supplies every fact it
needs, exactly like `DiagnosticEngine` only ever sees the evidence it's handed.

### Checks, in the order they run

1. **Advisory actions always pass.** `RECOMMEND_RESOURCE_ADJUSTMENT` and
   `RECOMMEND_CONFIG_FIX` never touch the cluster -- they only ever surface a
   suggestion -- so they're `ALLOWED` unconditionally, before any other check,
   even in `OBSERVE` mode.
2. **`OBSERVE` mode denies everything else.** Not "proposes but never executes" --
   denied outright, so no `RemediationAction` with mutating intent is ever even
   created while in this mode.
3. **Namespace allowlist** (`KUBESENTINEL_ACTION_ALLOWED_NAMESPACES`) -- `DENIED`
   if the target namespace isn't listed.
4. **Resource-name allowlist** (`KUBESENTINEL_ACTION_ALLOWED_RESOURCE_NAMES`,
   optional, empty = no extra restriction) -- `DENIED` if configured and the
   target name isn't listed.
5. **Resource-type consistency** -- `RESTART_DEPLOYMENT`/`ROLLBACK_DEPLOYMENT`/
   `SCALE_DEPLOYMENT`/`ADJUST_RESOURCES` only apply to `Deployment` targets. This
   project's reading of the spec's "whether the resource is managed by a
   controller" criterion: there's no controller to recreate a bare Pod from, so
   none of these actions make sense against one. `ADJUST_RESOURCES`
   (RESOURCE_OPTIMIZATION_DESIGN.md §14) reuses this exact check rather than
   relaxing it for StatefulSet/DaemonSet/Job/standalone-Pod recommendations.
6. **Cooldown** (`KUBESENTINEL_ACTION_COOLDOWN_SECONDS`) -- `DENIED` if the same
   action type was already proposed against the same `resource_uid` more recently
   than the cooldown window. Excludes previously `REJECTED` attempts (see
   TODO.md #12 -- found live, a rejected action never touched the cluster and must
   not block a later legitimate one).
7. **Per-incident budget** (`KUBESENTINEL_MAX_ACTIONS_PER_INCIDENT`) -- `DENIED` if
   this incident already has that many non-`REJECTED` actions recorded. Skipped
   entirely (never denies) when `incident_id` is `None` -- a standalone action
   (no owning Incident at all; see "Standalone actions" below) has no incident to
   bound against. The per-window budget and the cooldown above both still apply
   unconditionally either way.
8. **Per-window budget** (`KUBESENTINEL_MAX_ACTIONS_PER_WINDOW` /
   `KUBESENTINEL_ACTION_WINDOW_SECONDS`) -- `DENIED` if too many non-`REJECTED`
   actions (across any incident, *and* every standalone action) have been
   proposed within the rolling window.
9. **Mode-based outcome** (only reached if nothing above denied it):
   - `APPROVAL_REQUIRED` -> `REQUIRES_APPROVAL` for everything.
   - `CONTROLLED_AUTO` + `ROLLBACK_DEPLOYMENT`/`SCALE_DEPLOYMENT`/
     `ADJUST_RESOURCES` -> `REQUIRES_APPROVAL` always, even here -- higher-impact
     than a restart, matching the spec's explicit "approval-gated scaling."
     `ADJUST_RESOURCES` joins this list for the same reason: it changes running
     Pods' resource allocation, and this project has no correctness evidence yet
     strong enough to let it run unattended.
   - `CONTROLLED_AUTO` + `RESTART_DEPLOYMENT` -> `ALLOWED` only if **the diagnosing
     hypothesis itself says `auto_remediation_permitted=True`**, the incident is
     `HIGH`+ severity, the triggering hypothesis was `HIGH` confidence, *and* the
     target's desired replica count is known and greater than 1 -- otherwise
     `REQUIRES_APPROVAL`. A standalone action (no incident, so no hypothesis at
     all) can never satisfy this -- `hypothesis_confidence` is fixed at
     `INCONCLUSIVE` and `hypothesis_auto_remediation_permitted` at `False` for
     every one, so it always falls through to `REQUIRES_APPROVAL` regardless of
     action type.

### Why the hypothesis's own belief gates auto-execution

Found as a real gap while building Milestone 4, not designed in from the start:
severity and confidence alone aren't enough to decide a restart is *useful* --
they only say how sure the diagnosis is, not whether the diagnosed cause is
restart-shaped. Every rule through Milestone 2 sets `auto_remediation_permitted`
to `False` on every hypothesis it produces, and the policy engine now actually
reads that flag (`PolicyRequest.hypothesis_auto_remediation_permitted`) before
ever reaching `ALLOWED`. Verified live: a real `ROLLOUT_FAILED` incident
(`imagepull-demo`, HIGH severity, HIGH confidence) correctly stayed
`PENDING_APPROVAL` under `CONTROLLED_AUTO`, because the rule itself says a restart
won't fix a bad image reference.

### Why desired-replica-count gates auto-execution

Restarting a Deployment's only replica causes a real availability gap. An unknown
replica count is treated exactly like "could be 1" -- conservative by default,
never optimistic.

## The remediation executor (`remediation/executor.py::RemediationExecutor`)

The only code in this project permitted to mutate cluster state. Every mutating
method follows the same shape:

1. Re-read the live Deployment and verify its UID still matches what the action
   was proposed against -- directly implements the spec's "the workload changes
   before the action executes" test case. A mismatch aborts with
   `EXECUTION_FAILED`, never proceeds.
2. Perform exactly one, narrowly-scoped API call, under a hard timeout
   (`KUBESENTINEL_ACTION_EXECUTION_TIMEOUT_SECONDS`).
3. Never raise to the caller -- every failure (API error, resource-changed,
   anything unexpected) is caught and recorded as `execution_error` on the action.

**`RESTART_DEPLOYMENT`**: patches `spec.template.metadata.annotations
["kubectl.kubernetes.io/restartedAt"]` -- the exact mechanism `kubectl rollout
restart` uses.

**`SCALE_DEPLOYMENT`**: patches the Deployment's `scale` subresource.

**`ROLLBACK_DEPLOYMENT`**: lists the Deployment's owned ReplicaSets, reads each
one's `deployment.kubernetes.io/revision` annotation, and does a full
`replace_namespaced_deployment` with `spec.template` set to the target revision's
template -- **not** a merge patch. Found live (TODO.md #13): a merge patch cannot
correctly roll back to a template that *lacks* a field the current one has (e.g.
an added annotation), because an absent field in the patch body means "no change"
under merge semantics, not "clear it." Only a full replace actually removes it.

**`ADJUST_RESOURCES`** (RESOURCE_OPTIMIZATION_DESIGN.md §14): a merge PATCH,
like `RESTART_DEPLOYMENT` -- never a full replace like `ROLLBACK_DEPLOYMENT`,
since this never needs to *clear* a field, only set the specific ones a
recommendation proposed a number for. Every value in the patch body is a
plain `dict`/`str`, never a typed client object nested inside a dict, so
the `.to_dict()` footgun ROLLBACK's own history (TODO.md #11) warns about
cannot recur here. Kubernetes' own strategic-merge-patch semantics handle
the rest: `containers` merges by its `name` key (only the named container is
touched), and `resources.requests`/`resources.limits` are plain maps,
deep-merged key-by-key (setting `cpu` leaves an existing `memory` key, if
any, untouched).

**`RECOMMEND_RESOURCE_ADJUSTMENT` / `RECOMMEND_CONFIG_FIX`**: never call the
Kubernetes API. `remediation/recommendations.py::build_recommendation` maps a
hypothesis's `reason_code` to one of these two types and reuses its `explanation`
+ `recommended_next_steps` verbatim -- never inventing advice beyond what the
rule already said.

## The recovery verifier (`verification/verifier.py::RecoveryVerifier`)

Confirms whether a remediation action actually fixed the problem, via a fresh
Kubernetes API observation -- never by trusting that the action's own API call
returned success. Polls the target Deployment up to a deadline
(`KUBESENTINEL_VERIFICATION_DEADLINE_SECONDS`, default 60s) at an interval
(`KUBESENTINEL_VERIFICATION_POLL_INTERVAL_SECONDS`, default 2s) and returns one of
three outcomes, never collapsing the third into either of the first two:

- **`RECOVERED`** -- confirmed healthy by a fresh observation.
- **`FAILED`** -- confirmed still showing a failure signal (or the target no
  longer exists).
- **`INCONCLUSIVE`** -- the deadline was reached with neither a confirmed
  recovery nor a confirmed failure (e.g. a rollout still settling, or an
  unexpected error talking to the API). Also used for any unexpected error
  talking to the API -- `verify()` never raises to its caller.

Deliberately reuses Milestones 1-2's own `deployment_evidence`,
`has_failure_signal`, `is_confirmed_healthy` rather than inventing a third
definition of "is this Deployment healthy" -- except for one deliberate
exception, found necessary live:

### Why RESTART/ROLLBACK need a stricter check than SCALE

Found live, not designed in from the start: checking only `is_confirmed_healthy`
reported `RECOVERED` within 0.1s of triggering a real restart on
`remediation-test`, while `kubectl get pods` still showed the new Pod
`ContainerCreating` and both old Pods still `Running`. Kubernetes deliberately
keeps old Pods serving during a rolling update, so the Deployment's aggregate
ready/available counts can look fully satisfied via the *old* ReplicaSet well
before the new one has done anything -- `is_confirmed_healthy` is the right
check for "is this Deployment healthy *right now*, regardless of why" (the
passive incident-manager use case it was built for), but the wrong one for "did
*this specific restart* actually complete."

Fixed with a separate `_rollout_complete()` check, used for
`RESTART_DEPLOYMENT`/`ROLLBACK_DEPLOYMENT`/`ADJUST_RESOURCES` (all three start a
new rollout to a new template -- patching
`spec.template.spec.containers[].resources` is the same kind of template
change as a restart annotation or a rolled-back template, so
`ADJUST_RESOURCES` was added to this set for the same reason, not a new
one): also requires `updatedReplicas >= desired`, confirming the *new*
ReplicaSet -- not just any Pods -- caught up. This is exactly what `kubectl
rollout status` waits for. `SCALE_DEPLOYMENT` starts no new rollout, so it keeps
the simpler shared check. Re-verified live after the fix: took 11.5s, and the
pod list confirmed the new ReplicaSet had genuinely replaced the old one before
`RECOVERED` was reported.

## The remediation service (`remediation/service.py::RemediationService`)

Four operations, matching the spec's distinct plan/approve/execute/reject
lifecycle rather than collapsing any of them together:

- **`propose()`**: builds a `RemediationAction`, evaluates it against policy, and
  persists it. `DENIED` -> `REJECTED`. `REQUIRES_APPROVAL` -> `PENDING_APPROVAL`,
  stops there. `ALLOWED` (`CONTROLLED_AUTO` only) -> runs the executor+verifier
  inline immediately, since there's no human to wait for.
- **`approve()`**: marks a `PENDING_APPROVAL` action `APPROVED`. Deliberately does
  **not** execute it -- found necessary while building this milestone: Milestones
  4-5 had `approve()` execute inline, collapsing the spec's three distinct
  plan/approve/execute operations into two. Splitting them lets a human approve
  now and execute later (or never).
- **`execute()`**: runs an `APPROVED` action through the executor, then the
  verifier, then persists the final terminal status
  (`VERIFIED_RECOVERED`/`VERIFICATION_FAILED`/`VERIFICATION_INCONCLUSIVE`). An
  action that failed to even execute never reaches verification at all, per the
  spec's "never mark anything resolved on an action that didn't succeed." On a
  confirmed `RECOVERED`, `kubesentinel_recovery_duration_seconds` records the
  executed-to-verified elapsed time.
- **`dry_run()`**: evaluates the same `PolicyRequest` a real `propose()` would,
  against the *real* current action history (a realistic cooldown/budget
  preview) -- but builds, persists, and executes nothing. True zero side
  effects, not just "skips the executor."
- **`reject()`**: marks a `PENDING_APPROVAL` action `REJECTED` with a reason.

### Standalone actions (no owning Incident at all)

`propose_standalone()`/`approve_standalone()`/`execute_standalone()`/
`reject_standalone()`/`dry_run_standalone()` (RESOURCE_OPTIMIZATION_DESIGN.md
§14.1) mirror the five operations above exactly -- same policy evaluation,
same executor, same verifier -- for an action with no backing `Incident`
object, the common case for applying a `ResourceRecommendation` (most
recommendations have no incident at all; proactive right-sizing is the
primary value of that engine, not firefighting). `RemediationAction.
incident_id`/`PolicyRequest.incident_id`/`incident_severity` are `Optional`
specifically to make this possible without duplicating the state machine:
every existing incident-driven call site is unaffected (they already
always pass a real string). Persisted independently
(`IncidentRepository.save_standalone_action`/`get_standalone_action`, a
sibling table in the same SQLite file) rather than nested in an `Incident`'s
own `.actions` list -- there's no incident to nest it in. `list_actions_since`
unions both kinds, so the per-window budget genuinely bounds every mutation
this project makes, not just the incident-driven half of them.

Every one of the five incident-driven methods above takes an `Incident`
object and plain arguments and is otherwise API/CLI-agnostic -- both
`api/app.py`'s `/incidents/{id}/actions/plan|approve|execute|reject`
endpoints and the CLI's `plan`/`approve`/`execute`/`reject`/`dry-run`
commands call these directly, as plumbing, not new logic. The five
standalone siblings are reached the same way through their own
`/actions/{action_id}/...` routes and `recommendation-apply`/`action`/
`action-approve`/`action-execute`/`action-reject` CLI commands instead.
`approve()`/`execute()`/`propose()` (when it executes inline) block the
caller synchronously for up to the verification deadline (this project
has no background job queue) -- a deliberate, documented tradeoff for a
synchronous, portfolio-scoped tool, not a hidden surprise. Same for their
standalone counterparts.

## The API and CLI surfaces (Milestone 6)

`POST /incidents/{id}/actions/plan|{action_id}/approve|{action_id}/execute|
{action_id}/reject`, every one gated by `require_api_token`
(`api/app.py::require_api_token`): an **unset** `KUBESENTINEL_API_TOKEN` returns
`503` (disabled), never `200` -- there is no code path where "no token
configured" is treated as "no auth needed," matching the spec's "do not expose
an unauthenticated, unrestricted remediation endpoint" literally. A configured-
but-wrong token is `401`. `ActionNotFoundError`/`InvalidActionStateError` from
the service map to `404`/`409`.

The CLI's `dry-run`/`plan`/`approve`/`execute`/`reject` commands build the same
`PolicyEngine`/`RemediationExecutor`/`RecoveryVerifier`/`RemediationService`
objects directly against the live cluster and the local SQLite store -- no HTTP
round-trip, and they work whether or not the API server is running, exactly like
every other CLI command in this project.

## Verified live against the real kind cluster (not just unit tests)

Using the dedicated `remediation-test` Deployment (2 replicas, healthy, exists
only for this):

- **Scale** 2 -> 3: confirmed via a fresh `read_namespaced_deployment` call that
  `spec.replicas` actually became 3.
- **Restart**: confirmed a new `resourceVersion` and a new ReplicaSet with the
  restart annotation present.
- **Rollback**: confirmed, after the fix above, that the restart annotation was
  genuinely gone from the live Deployment and the original ReplicaSet scaled back
  up to 2/2 -- not just that the action reported `EXECUTED`.
- **The cooldown and per-incident budget both fired for real** during this same
  run (not staged), leading directly to the two policy-engine fixes in TODO.md
  #12.
- **Honest failure case**: approved a human-override restart against a real
  `ROLLOUT_FAILED` incident the rule said wouldn't help: confirmed the Pod was
  still `ImagePullBackOff` afterward.
- **Recovery verification**: a real restart against `remediation-test`, verified
  end to end with the project's real (non-test-shortened) 60s/2s deadline/poll
  settings -- `VERIFIED_RECOVERED` only reported once the new ReplicaSet had
  genuinely replaced the old one (see "Why RESTART/ROLLBACK need a stricter
  check" above for the bug this caught).
- **RBAC boundary**: confirmed with `kubectl auth can-i` against the actual
  `kubesentinel-remediator` ServiceAccount (not the admin kubeconfig every other
  live check ran under) that rollback's `update` verb requirement is real --
  `no` before adding it to the Role, `yes` after.
- **Auth boundary (Milestone 6)**: with `KUBESENTINEL_API_TOKEN` unset, `curl`
  against `POST /incidents/{id}/actions/plan` returned `503`, never `200` or an
  unauthenticated success. With a token configured, a wrong `Authorization`
  header returned `401`; the correct token let a real `plan` -> `approve` ->
  `reject` sequence run end to end against a real diagnosed `crashloop-demo`
  incident over HTTP.
- **CLI plan/approve/execute (Milestone 6)**: `kubesentinel plan`, `approve`,
  and `execute` run directly (no HTTP) against `remediation-test` and the real
  cluster, exercising the same split-lifecycle service methods the API uses.
  This is also where the click/typer crash (TODO.md #16) and the transient
  `Available=False` false-FAILED (TODO.md #17) were both found and fixed.
- **`ADJUST_RESOURCES` standalone apply (RESOURCE_OPTIMIZATION_DESIGN.md
  §14), via the real CLI against a real throwaway Deployment**: with
  `policy_mode=OBSERVE` (this project's own default), `recommendation-apply`
  correctly came back `REJECTED` ("policy mode is OBSERVE"), confirming
  the new action type is gated by the same default every other action type
  already is. Switching to `APPROVAL_REQUIRED`: `recommendation-apply` ->
  `PENDING_APPROVAL`; `action-approve` -> `APPROVED` (confirmed the real
  Deployment's resources were still untouched at this point); `action-execute`
  -> `VERIFIED_RECOVERED`, and a fresh `kubectl get deployment` confirmed only
  the proposed `requests.cpu` (50m -> 80m) had changed -- `requests.memory`,
  `limits.cpu`, `limits.memory`, the image, and the command were all
  byte-for-byte untouched, confirming the merge-patch isolation claim for
  real, not just in a mocked unit test. A second `action-execute` on the same
  (now-`VERIFIED_RECOVERED`) action correctly refused ("not APPROVED -- cannot
  execute"). **A real bug caught in this exact run, not by any unit test**:
  the first (policy-denied) attempt had set `applied_action_id` on the
  recommendation even though the resulting action was `REJECTED` and never
  touched the cluster -- permanently locking that recommendation out of ever
  being retried. Fixed immediately (`applied_action_id` is now only set when
  the action is not `REJECTED`, mirroring the exact "a REJECTED action never
  counts" discipline `policy/engine.py`'s own cooldown/budget checks already
  apply) and covered by a new regression test, not just patched and moved on.

215 unit tests cover every branch of the policy engine, executor, service,
verifier, and the API's auth dependency (942 after this milestone's own
additions -- see TODO.md's own entry for the exact count).

## What already exists

- `models/action.py::ActionType` is a closed enum. There is no "run arbitrary
  command" action type, and adding one would be a deliberate, reviewable change to
  that file -- not something a rule, an API caller, or an AI integration could
  introduce dynamically.
- `models/action.py::ActionStatus` encodes the full lifecycle explicitly:
  `PROPOSED -> PENDING_APPROVAL -> APPROVED -> EXECUTING -> EXECUTED -> VERIFYING ->
  VERIFIED_RECOVERED` (or `VERIFICATION_FAILED`/`VERIFICATION_INCONCLUSIVE`), with
  `REJECTED` and `EXECUTION_FAILED` as explicit terminal states. Milestone 5 now
  reaches every one of these states in practice, not just as scaffolding.

## Commitments

- **Narrowly scoped RBAC**, added only alongside the code that needs it -- see
  RBAC_AND_THREAT_MODEL.md.

## Not yet implemented

The API and CLI surfaces exist and are live-verified (above), but nothing
currently calls `RemediationService.propose()` from the live diagnosis
pipeline automatically -- every live exercise of it so far has been a
manually-constructed incident (via a standalone script), a human-approved
override on a real diagnosed one, or a human issuing `plan`/`approve`/`execute`
themselves via the CLI or API. There is no code path today where a diagnosed
incident triggers remediation without a human (or, in `CONTROLLED_AUTO`, the
policy engine's own explicit `ALLOWED` decision) in the loop at propose time.
Do not read this document as describing an unattended end-to-end flow;
TODO.md tracks exactly what's real.

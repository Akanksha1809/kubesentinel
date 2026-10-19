# Design decisions

## Why rule-based diagnosis, not ML/LLM, for the core engine

The spec explicitly requires the first release to work without an AI model, and for
AI (when added later) to never determine authorization. Beyond that requirement:
deterministic rules are the only approach where a confidence level and an evidence
citation mean something verifiable. "HIGH confidence, see Evidence abc123" is a
falsifiable claim you can check against the actual Kubernetes object; "the model is
90% confident" is not, unless the model's reasoning is itself constrained to cite
evidence the same way -- which is exactly what an LLM layered on top (Milestone 11)
would be restricted to doing: summarizing and explaining *already-computed*
deterministic hypotheses, never inventing or re-ranking them.

## Why resource UID, not name+namespace, for incident identity

Kubernetes names are reused constantly (a Deployment deletes and recreates Pods
routinely, with the same name pattern); UID is the only identifier the API server
guarantees is unique across an object's entire history. Deduping on name+namespace
would silently merge an old, already-resolved incident with a brand-new unrelated
failure that happens to reuse the same name. The cost (documented in TODO.md) is that
UID-based dedup doesn't automatically follow a resource across a Deployment rollout,
since the rollout's new Pods get new UIDs -- that's a real limitation, not swept under
the rug, and the fix (Deployment/ReplicaSet-level incident correlation) is scoped into
Milestone 2 rather than being bolted on ad hoc now.

## Why SQLite, not Postgres, for persistence

This is explicitly a local, single-process, portfolio-scoped tool: no deployment
topology where multiple KubeSentinel instances write concurrently. SQLite needs zero
setup, which matters for "runnable on my Mac without AWS." The repository interface
(`persistence/interface.py`) exists specifically so this choice is reversible without
touching the incident manager, API, or CLI -- `SQLiteIncidentRepository` is the only
file that would need a Postgres sibling.

## Why evidence as JSON blobs per row, not a normalized schema

Each incident's full state (evidence, diagnosis history, actions, audit log) is
serialized as one JSON blob via `Incident.model_dump_json()` / `model_validate_json()`,
not spread across normalized tables. For a local, single-reader-at-a-time tool, the
complexity of migrations and joins across a half-dozen tables isn't justified by
anything the API actually needs (it always reads/writes whole incidents, never a
cross-incident evidence query). The indexes that exist (`resource_uid`, `status`,
`namespace`) are exactly the three things `find_open_by_resource_uid` and `list()`
filter on -- added because they're used, not preemptively.

## Why confidence ranking uses rule registration order as a tiebreak, not a
separate "specificity" field

`OOMKilledRule` is listed before `CrashLoopBackOffRule` in
`diagnostics/engine.DEFAULT_RULES` specifically so that when both fire at *equal*
confidence, the more specific root cause (OOM) ranks above the generic symptom
(CrashLoopBackOff). Confidence itself is never faked to force a ranking -- when OOM
is only inferred (MEDIUM) and CrashLoopBackOff is directly observed (HIGH), the
observed symptom legitimately outranks the inferred cause, and the engine lets that
happen. Adding a separate specificity score would let a rule *claim* more certainty
than its evidence supports just to win the ranking, which is exactly the shortcut this
project is trying not to take.

## Why the observer handles a terminating Pod by skipping it entirely, not by
trying to interpret its state

Found live, not anticipated: a Pod with `deletionTimestamp` set can transiently
report container statuses with no "bad" waiting/termination reason while torn down,
which the health classifier would otherwise read as recovery and prematurely resolve
a real incident. The fix doesn't try to special-case every transient shape a
terminating Pod's status can take (fragile, and games-of-whack-a-mole-prone);
it treats `deletionTimestamp != None` as "this Pod is not a source of health
information right now" and skips it outright, full stop.

## Why `_ListWatchLoop` runs one independent thread per configured namespace,
not one merged watch stream

**Closed -- was a known limitation, named explicitly rather than silently
worked around; see TODO.md's Milestone-1 known-limitations list, item 3, and
the later milestone that closed it.** `PodWatcher`'s (and every other
namespaced watcher's) periodic resync LISTs every configured namespace
correctly, but its live WATCH stream used to only ever follow the first
namespace in the list -- so namespaces past the first were picked up at most
once per resync interval (default 300s), not on every change. A single
`watch.Watch()` stream can only ever follow one namespace at a time (no
Kubernetes API supports watching several arbitrary namespaces over one
stream), so there was no way to fix this by tuning the existing stream --
it needed genuinely independent streams, one per namespace.

Fixed by giving each configured namespace its own, fully independent
list-then-watch-then-resync loop, run concurrently in its own daemon thread,
all sharing one `stop_event` (`_ListWatchLoop.run`/`_run_one_namespace` in
`observer/watchers.py`). Each namespace's own relist/backoff/410 handling is
therefore already isolated by construction -- a slow or stuck namespace
can never block or delay another's thread, confirmed live by making one
namespace's loop block indefinitely while a second namespace's events still
arrive. Safe under this project's own existing concurrency model, not a new
one: `ObserverService` already invokes `IncidentManager.observe()`
concurrently from one thread per *watcher type* (Pod/Event/Deployment/PVC/
EndpointSlice), and `SQLiteIncidentRepository` already guards its shared
connection with its own lock; adding one more thread per *namespace* within
a single watcher type introduces no new race, because two threads for the
same watcher type always observe disjoint resources (a Pod's UID belongs to
exactly one namespace, so no two per-namespace threads of the same watcher
ever call `observe()` for the same resource). For 0 or 1 configured
namespaces -- the common case, including the single-namespace lab -- `run()`
still executes directly on the calling thread, identical to before; the
extra threading only exists for 2+ namespaces.

## Why the policy engine's checks run as an ordered list, first-match-wins, not
a single combined boolean expression

`PolicyEngine.evaluate()` runs `_check_mode_not_observe`,
`_check_namespace_allowlist`, `_check_resource_name_allowlist`,
`_check_resource_type_matches_action`, `_check_cooldown`,
`_check_per_incident_budget`, `_check_per_window_budget` in that specific
order, each returning early on the first `DENIED`/`REQUIRES_APPROVAL` it finds.
This makes the policy's `reason` string always name the *first* actual blocker
-- which matters because a human reading a rejected action's audit log needs
to know which specific gate stopped it, not just that "some check failed."
A single boolean expression could return the same final `True`/`False` but
would lose that traceability, and silently invert which check "wins" the
instant anyone reorders the clauses.

## Why `RECOMMEND_*` action types bypass every policy check

`ActionType.RECOMMEND_RESOURCE_ADJUSTMENT`/`RECOMMEND_CONFIG_FIX` never touch
the Kubernetes API at all -- they only ever produce advisory text from the
evidence/hypothesis already on hand. `PolicyEngine.evaluate()` short-circuits
to `ALLOWED` for both, before the mode/allowlist/cooldown/budget checks ever
run, because none of those checks exist to gate something that carries zero
remediation risk; applying them anyway would just add friction (and
budget/cooldown consumption) to a code path that can never affect the cluster.

## Why a `REJECTED` action is excluded from cooldown and budget accounting

Found live (TODO.md #11/#12): if a rejected action still counted against the
per-incident/per-window budgets and cooldowns, a handful of harmless, unrelated
rejections (e.g. from an active cooldown on a *different* action type) could
permanently exhaust an incident's remediation budget without a single action
ever having actually reached the cluster. A `REJECTED` action, by definition,
never executed -- it never started a cooldown and never consumed budget meant
to bound *real* attempts against the cluster, so all three checks explicitly
filter it out. `EXECUTION_FAILED` is deliberately *not* excluded the same way:
it did reach the cluster, and repeated-attempt budgeting exists precisely to
bound that case.

## Why `propose`/`approve`/`execute` are three separate `RemediationService`
methods, not two

The spec's remediation lifecycle is explicitly plan -> approve -> execute --
three distinct human-meaningful operations. An earlier version of this
project's `approve()` executed inline (collapsing approve+execute into one
call), which works but silently drops the "approve now, execute later (or
never)" option the spec's three-step framing implies. Splitting them is a
deliberate, zero-ambiguity mapping: `propose()` builds+evaluates+persists,
`approve()` only flips `PENDING_APPROVAL -> APPROVED` and stops, `execute()` is
the only path that reaches the executor for a human-approved action. A
`CONTROLLED_AUTO`-allowed action never needs a separate `execute()` call --
`propose()` runs it inline immediately, since by definition there's no human
to wait for.

## Why the recovery verifier has a stricter "is this definitively broken"
check than the passive incident manager's

`has_failure_signal()` (passive, Milestones 1-2) is tuned to open an incident
promptly from a single bad observation, including a bare
`Available=False/MinimumReplicasUnavailable` -- a reasonable thing to flag when
observed with no other context. The *active* recovery verifier is, by
definition, watching a transition it just triggered, where that exact
condition is routinely and briefly true during a normal scale-up or rollout.
Reusing the passive rule's definition of "broken" for the verifier's FAILED
path produced a real false failure live (TODO.md #17: a routine
`SCALE_DEPLOYMENT` 2->3 on a perfectly healthy Deployment reported FAILED).
The fix is a second, separate, stricter check
(`_deployment_definitively_stuck()`) used only by the verifier, trusting only
`ProgressDeadlineExceeded` -- never inventing a third shared definition, but
also never conflating "enough to open an incident" with "enough to call a
just-taken action a confirmed failure."

## Why `incidents_active` needs an explicit startup reconciliation, not just
inc()/dec()

Prometheus `Counter`/`Gauge` objects are process-local by construction --
restarting the process resets them to zero regardless of what's actually true
in the (persistent, cross-restart) SQLite repository. `IncidentManager`'s
`_open_new`/`_resolve` only run on a *freshly detected* transition, so an
incident already open in the database before the current process started never
passes through either, leaving the gauge silently wrong until (if ever) that
specific incident's category happens to get a fresh detection. Found live on a
long-running demo server (TODO.md #18). The fix,
`IncidentManager.reconcile_metrics()`, is called once at API startup rather
than relying on `inc()`/`dec()` alone -- a deliberate acknowledgment that a
gauge meant to reflect *current state* needs an explicit reconciliation against
the source of truth on every process start, not just incremental deltas
assumed to always start from zero.

## Why `Incident.active_metric_category` exists as a field separate from
`category`

**Closed -- was a known limitation, named explicitly in TODO.md's Milestone-1
list, item 4: "`incidents_active` gauge can drift if an incident's diagnosed
category changes between open and resolve (decrement uses the category *at
resolve time*, which may differ from the one used at increment time)."** A
real, not theoretical, gap: `_open_new` incremented whichever category
`_apply_diagnosis` computed at open; `_resolve`/`_supersede` decremented
whichever category `incident.category` held at resolve time -- and nothing
in between ever adjusted the gauge if diagnosis re-categorized an
already-open incident as more evidence arrived (e.g. `CRASH_LOOP_BACKOFF` ->
`OOM_KILLED` once a later observation's termination evidence revealed the
real cause). The original category's slot never decremented (stuck "active"
forever); the new category's slot would later decrement without this
incident ever having incremented it.

The fix tracks, on the `Incident` itself, which category label the gauge
currently credits this incident's open slot to -- `active_metric_category`,
kept in sync by `IncidentManager._rebalance_active_metric` on every
`_open_new`/`_update_existing` call (dec the old label, inc the new one,
only when they actually differ), and read back (never re-derived from
`category`) by `_resolve`/`_supersede` when decrementing. `reconcile_metrics()`
backfills this field for any incident persisted before it existed (defaults
to `None`) -- without that backfill, the very first `_update_existing` call
on an old row would see `None != category` and wrongly treat an unrelated
observation as a fresh category change, double-counting the gauge instead of
leaving it alone. Confirmed live against the real cluster: a Pod made to
transition `CRASH_LOOP_BACKOFF` -> `ERR_IMAGE_PULL` while remaining the same
open incident correctly showed the first category's gauge drop back to 0
and the second category's gauge read exactly 1, via the real `/metrics`
endpoint.

## Why `RemediationService.propose()` gets a dedicated lock instead of a
broader locking scheme

Found live by the exact test written to check it (TODO.md #20): concurrent
`propose()` calls on the same incident can all read "no actions yet" before any
of them persists, bypassing the per-incident budget entirely. The fix is a
single `threading.Lock` scoped to `propose()` specifically -- not a lock around
every `RemediationService` method, and not a database-level
optimistic-concurrency token. `approve()`/`execute()`/`reject()` already
operate on one already-identified `action_id` rather than deciding a *new*
action's admission against a read of recent history, and `execute()` in
particular can block for the full verification deadline (up to 60s by
default) -- serializing everything behind one lock would mean one slow
`execute()` blocks every other `propose()`/`approve()` call for up to a
minute, a worse tradeoff than the narrower race it would additionally close.
This is a deliberately scoped fix for the concurrency property this project
actually tested, not a claim that every possible race across these four
methods is closed -- the remainder is named explicitly in TODO.md rather than
assumed away.

## Why the AI backend is a local Ollama server, not a cloud API

The spec requires the first release to work without any AI model, and for AI,
once added, to never determine authorization. Beyond the letter of that
requirement: a cloud API, even a "free tier" one, still needs an account, an
API key to protect, a network dependency, and eventually a bill past some
quota -- none of which "runs on my Mac" can promise. A local Ollama server is
genuinely free (no account, no key, no outbound call, no quota) and keeps the
project's own "no paid cloud services" framing true of its optional AI feature
too, not just its required path. `AiClient` is a `Protocol`
(`ai/client.py`), so a cloud backend could be added later as a second
implementation without `summarizer.py` or anything that calls it changing --
this is a deliberate choice to ship the free option first, not a claim that a
cloud backend could never be added.

## Why `summarize_incident()` takes an already-built `Incident`, never raw
cluster objects

`ai/summarizer.py::build_prompt()` only reads `Hypothesis.explanation`/
`missing_evidence`/`confidence` and `RemediationAction.action_type`/`status`/
`verification.reason` -- the same structured fields a human already sees via
`kubesentinel evidence`/`kubesentinel incidents`, never a raw `V1Pod`, Event
message, or annotation. This is the same "untrusted input" boundary
RBAC_AND_THREAT_MODEL.md already draws around `inspector.py`'s evidence
extraction, applied one layer further out: even if a workload could get
attacker-controlled text into a Pod annotation or Event message (already true
today, and already never executed or templated anywhere), that text has no
path into an AI prompt, because the prompt is built from the diagnostic
engine's own structured conclusions, not from the raw fields those
conclusions were derived from.

## Why the resource optimization engine has no apply/execute endpoint

Unlike `RECOMMEND_*` action types (above), which exist within an already-built
execution lifecycle and simply opt out of it, the optimization engine has no
execution lifecycle to opt out of at all -- there is no `ActionType`, no
executor method, and no RBAC write verb anywhere in `optimization/` that
could patch a workload's resources. The spec's own instruction is to keep
analysis and execution clearly separated and to require explicit approval
before any future apply path; exposing an endpoint that implied one already
existed would be the "misleadingly-named endpoint for unimplemented
behavior" the spec explicitly warns against, which is a stronger reason to
leave it out than merely "we haven't gotten to it yet." A future apply path
is specified (RESOURCE_OPTIMIZATION_DESIGN.md §6) to reuse the *existing*
`PolicyEngine`/`RemediationExecutor` lifecycle, not a second approval system
built from scratch for this engine alone.

## Why CPU uses a percentile of typical demand and memory uses the peak,
never the average for either

A proposed *request* exists to answer "how much does this container
typically need," which a percentile of demand (default p95) answers
directly; the bare average is pulled down by idle periods a real workload
still needs headroom for; the bare max is pulled up by one-off spikes that
would make every proposal too conservative to be useful. Memory gets a
stricter rule than CPU for a reason specific to what each resource actually
does when exceeded: CPU usage above a limit is throttled (survivable,
recoverable); memory usage above a limit gets the container OOM-killed
(not recoverable). Proposing a memory number from anything less than the
observed *peak* risks proposing a limit the workload has already, in the
observed window, exceeded -- which is why memory uses p99-of-peak and CPU
uses p95-of-typical, a structural difference enforced by reading different
fields off `UsageSummary` (`cpu_typical_millicores` vs. `memory_peak_bytes`),
not just differently-worded reasoning text that could silently drift from
the underlying math.

## Why a recommendation needs at least 3 samples spanning 5 minutes before
proposing any number at all

Found worth enforcing in code, not just policy, after the spec's own
instruction not to recommend aggressive downsizing from a single
observation: a single metrics-server snapshot is one instant, not a
pattern, and proposing a number from it would be indistinguishable from
guessing. `EvidenceQuality` has two tiers below the resize-eligible
threshold (`INSUFFICIENT`, `SNAPSHOT_ONLY`) specifically so a workload with
too little history still gets a row (visibly "considered"), just with
`proposed_requests=None` and a `reasoning` string saying why -- the same
"flag, never silently drop" principle `safety.py` uses for unhealthy
workloads, applied to insufficient evidence instead of to workload health.

## Why recommendation supersession only ever touches a still-`NEW` prior row

Each `analyze()` run persists a fresh recommendation for every workload it
looked at; without any rule for what happens to the *previous* run's row for
the same workload/container/category, recommendations would accumulate
forever with no way to tell which one is current. The rule is deliberately
narrow: a prior row still in `NEW` status is marked `EXPIRED` (superseded by
a fresher analysis), but a prior row already `REVIEWED`/`APPROVED`/
`REJECTED` is left completely untouched. A human's decision is a historical
record of what was decided and why at the time -- a later, unrelated
`analyze()` pass re-running the same math does not get to silently overwrite
that record just because it happened to look at the same workload again.

## Why a recommendation's `EXPIRED` status is computed at read time, not
flipped by a background job

`expires_at` exists so a recommendation is never acted on once stale (the
spec's "never apply a stale recommendation," read broadly as "never even
display one as if it were current"). The straightforward implementation --
a periodic job scanning for expired rows and updating them -- would be an
unbounded background task this project's own persistence design
(TODO.md/RESOURCE_OPTIMIZATION_DESIGN.md §11) explicitly avoids, and it adds
a window where a row reads as `NEW` after it's actually expired, just because
the job hasn't run yet. Computing the effective status in
`_with_effective_status()` on every `GET`/review check instead means there
is no such window, and no scheduled task to keep correctly running --
reading a stale recommendation can never itself mutate storage, and nothing
needs to "catch up" on startup.

## Why the `ACTIVE_INCIDENT` check looks at a workload's own UID *and* every
current Pod's UID, not just one

An incident in this project can be opened against either kind of resource,
depending on which diagnostic rule found it first: a Pod-level
`CrashLoopBackOff` opens against the Pod's own UID; a Deployment-level
`UNSATISFIED_REPLICAS`/`ROLLOUT_FAILED` opens against the Deployment's UID.
Checking only the workload's own UID would miss a live, real incident on one
of its Pods; checking only Pod UIDs would miss one opened directly against
the controller. `OptimizationService._open_incident_ids_for()` checks both
sets (deduped before querying, since a standalone Pod's `workload_uid` is
the same value as its own entry in `pod_uids`) rather than guessing which
kind a given incident is more likely to be opened against.

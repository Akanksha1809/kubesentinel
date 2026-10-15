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

## Why `watch_namespace_list` with multiple entries is a known limitation, not
silently worked around

`PodWatcher`'s periodic resync LISTs every configured namespace correctly, but its
live WATCH stream only follows the first namespace in the list -- so namespaces past
the first only get picked up once per resync interval (default 300s), not on every
change. Building a correct multi-namespace merge (independent watch streams per
namespace, a shared resync timer, correct handling of one stream's 410 while others
stay healthy) is real complexity the single-namespace lab doesn't need yet. Rather
than hack in a silent partial fix, this is named explicitly in TODO.md as the thing to
address before `KUBESENTINEL_WATCH_NAMESPACES` is used with more than one value.

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

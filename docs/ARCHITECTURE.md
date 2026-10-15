# Architecture

## Pipeline (current, Milestones 1-9)

```
                 +-------------------+
 kube-apiserver  |  Pod/Event/Deploy-|  list-then-watch per resource type, 410 Gone
  (watch/list)   |  ment/PVC/Node/   |  -> relist, periodic resync, exponential
                 |  EndpointSlice    |  backoff on error (observer/watchers.py)
                 |  watchers         |
                 +---------+---------+
                           |
                           v  typed client object (or dict, EndpointSlice only)
                 +-------------------+
                 |  inspector.py     |  pure functions: object -> list[Evidence]
                 |  (observer/)      |  (no network, no side effects)
                 +---------+---------+
                           |
                           v  list[Evidence]
                 +-------------------+
                 | IncidentManager   |  dedup by resource UID (any kind): finds or
                 | (incidents/)      |  opens an Incident, runs the diagnostic
                 +---------+---------+  engine, classifies health, detects recovery
                           |
              +------------+---------------------------+
              v                                         v
   +-------------------+                      +-------------------+
   | DiagnosticEngine  |                      | SQLiteIncidentRepo|  upsert by
   | (diagnostics/)    |                      | (persistence/)    |  incident_id,
   +-------------------+                      +---------+---------+  JSON blob/row
                                                         |
                            +----------------------------+----------------------------+
                            v                                                         v
                 +-------------------+                                     +-------------------+
                 |  FastAPI + CLI    |  read endpoints + token-gated       |  RemediationService|
                 |  (api/, cli/)     |  plan/approve/execute/reject/       |  (remediation/)    |
                 |                   |  dry-run + /metrics                |  propose/approve/  |
                 +-------------------+                                     |  execute/reject/   |
                                                                             |  dry_run           |
                                                                             +----------+----------+
                                                                                        |
                                                              +-------------------------+-------------------------+
                                                              v                         v                         v
                                                   +-------------------+     +-------------------+     +-------------------+
                                                   |  PolicyEngine     | --> | RemediationExecutor| --> | RecoveryVerifier  |
                                                   |  (policy/)        |     | (remediation/)      |     | (verification/)   |
                                                   |  mode/allowlist/  |     | restart/rollback/   |     | polls the live    |
                                                   |  cooldown/budget  |     | scale/recommend-*   |     | cluster, never    |
                                                   +-------------------+     +-------------------+     |  trusts the       |
                                                                                                         |  action's own     |
                                                                                                         |  success          |
                                                                                                         +-------------------+

Prometheus metrics (metrics/) are emitted from every box above with a decision
to record: incidents detected/active, diagnostic/recovery duration, observer
errors, remediation attempts/successes/failures, actions requiring approval --
all bounded-cardinality labels (category/action_type/reason/component), never
an incident ID or resource name. Exposed at GET /metrics; see observability/
for a scrape config and dashboard.
```

## Why this shape

**Evidence is a first-class type, not a side effect of diagnosis.** Every fact a
rule uses is a `kubesentinel.models.evidence.Evidence` object with a `kind`, a
`source_field` naming exactly which part of the Kubernetes API response it came
from, and a `data` dict. Rules never read the raw API object (`V1Pod`,
`V1Deployment`, the `EndpointSlice` dict, ...) -- they only ever see
`list[Evidence]`. This is what makes "distinguish observed facts from inferred
hypotheses" an enforceable property rather than a design aspiration: a rule that
wants to claim something has to point at a specific `Evidence.id` in
`supporting_evidence`, and anything it couldn't verify goes in `missing_evidence`
instead of being silently assumed.

**Diagnostic rules are pure functions** (`list[Evidence] -> list[Hypothesis]`),
with no network calls, no cluster access, no randomness -- this is what makes
the bulk of this project's 218 unit tests run in under two seconds with no
cluster: a rule test just builds `Evidence` (via real `kubernetes.client` model
objects wrapped by `inspector.py`, or plain dicts for the one resource with a
client bug -- see TROUBLESHOOTING.md) and asserts on the `Hypothesis` list.

**Incidents are keyed by resource UID, not name+namespace, and dedup works
across resource kinds, not just Pods.** Kubernetes itself treats UID as the
only stable identity for an object across its lifetime. A Pod *and* its owning
Deployment are tracked as two independent incidents (different UIDs, different
kinds) when both show a failure signal -- `IncidentManager.observe()` doesn't
care what kind a `ResourceRef` is, only its UID, which is also why
`ROLLOUT_FAILED` (Deployment-level) and `CRASH_LOOP_BACKOFF` (Pod-level) can
coexist as separate incidents for the same underlying failure, each citing its
own evidence. See TODO.md's Known limitations for where this still breaks down
(a Deployment rollout creates new Pod UIDs, so a Pod-level incident doesn't
auto-correlate across a rollout fix).

**Diagnosis, policy, remediation, verification, persistence, and transport are
separate layers**, each knowing only about the layer(s) immediately adjacent to
it: `DiagnosticEngine` knows nothing about SQLite or the cluster; `PolicyEngine`
never touches the Kubernetes API, the database, or the clock on its own --
every fact it needs is handed to it in a `PolicyRequest`, which is exactly what
makes every branch of it unit-testable without mocking a cluster;
`RemediationExecutor` is the *only* code in the project permitted to mutate
cluster state, and only through the closed `ActionType` enum's specific
operations; `RecoveryVerifier` is a separate concern from both of them,
confirming an action's effect with a fresh API read rather than trusting the
executor's own "it returned 200" as truth. `RemediationService` is deliberately
where this stops being side-effect-free -- it's the first (and only) module that
wires policy's decision to the executor and verifier. `SQLiteIncidentRepository`
sits behind `persistence/interface.py` specifically so swapping in PostgreSQL
later touches one file, not the manager, policy engine, remediation service,
API, or CLI.

**The watch loop mirrors how Kubernetes informers actually work**, not a
polling loop: LIST to get a resourceVersion baseline, WATCH from that
resourceVersion, treat HTTP 410 (Gone -- the resourceVersion aged out of the
API server's watch cache) as a signal to relist from scratch rather than an
error to just retry, and force a periodic resync even on an otherwise-healthy
stream as a safety net against any missed event. `observer/backoff.py` is
deliberately a pure function so the reconnect math is unit-tested without
spinning up a fake watch stream; `tests/integration/test_watch_resync.py`
additionally confirms the resync boundary actually fires against a real API
server, not just that the math is right.

**The remediation service's three-call lifecycle (`propose`/`approve`/
`execute`) matches the spec's distinct plan/approve/execute operations rather
than collapsing any two of them.** `approve()` deliberately does not execute --
a human can approve now and execute later (or never) -- which is also why
`execute()` exists as its own method rather than being folded into `approve()`.
`dry_run()` is a fourth, side-effect-free sibling: it evaluates the same policy
decision a real `propose()` would, against the real current action history (a
realistic cooldown/budget preview), but builds, persists, and executes nothing.

**Concurrency is handled narrowly, where it was actually shown to matter, not
everywhere defensively.** `propose()`'s read-recent-actions -> decide ->
persist sequence is guarded by a single process-wide lock (found necessary by
an actual concurrency test, not added speculatively -- see TODO.md #20); the
SQLite repository has its own lock around each individual read/write. Nothing
else in this single-process, local-tool design is currently lock-protected,
and that remaining gap (two different callers racing `approve()`/`execute()`/
`reject()` on the same incident) is named explicitly as a known limitation
rather than silently assumed safe.

**AI summarization (`ai/`) sits off to the side of every decision path, never
in it.** `ai/summarizer.py::summarize_incident()` takes an already-built
`Incident` and an `AiClient` and returns text -- it has no reference to
`ClusterClients`, the repository, or `PolicyEngine`, so there is no path from
a model's output back into a diagnosis, a policy decision, or an executed
action, even in principle. `AiClient` is a `Protocol` the same way `Watcher`
is in `observer/watchers.py`: `OllamaClient` (the only implementation) talks
to a local server over plain HTTP, no API key, no outbound network call, no
cost. Disabled by default; every other layer above works fully without it.

## What's deliberately not built

- **Multi-namespace watch correctness beyond resync cadence** -- see
  DESIGN_DECISIONS.md.
- **DNS/connectivity diagnosis** -- the spec itself qualifies this category
  ("when the evidence supports it"); passive evidence alone gives too weak a
  signal without an active probe, which is out of scope. See
  DIAGNOSTIC_RULES.md.
- **Unattended auto-remediation beyond a plain restart** -- the policy engine's
  `CONTROLLED_AUTO` mode can only ever auto-execute `RESTART_DEPLOYMENT`, and
  only when the diagnosing rule itself believes a restart will help (which, as
  of every rule implemented today, none of them do -- see
  REMEDIATION_SAFETY.md).
- **AI beyond summarization** -- `ai/summarizer.py` only ever explains an
  already-computed diagnosis in plain English; it never produces a new
  hypothesis, never ranks or re-ranks anything, and has no path to policy or
  remediation. A cloud backend (behind the same `AiClient` Protocol) was
  deliberately not added -- the local Ollama backend is genuinely free, which
  a cloud API, even with a free tier, is not.

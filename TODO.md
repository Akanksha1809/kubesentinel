# TODO / Milestone tracker

Status is updated only after the described work is actually implemented and verified
(tests executed, or a live kind cluster check run) in this session -- not on intent.

## Milestone 0: Scaffold -- DONE

- [x] git repo, pyproject.toml, .gitignore, .env.example
- [x] Core Pydantic models: Evidence, Hypothesis, DiagnosisResult, ResourceRef, Incident, RemediationAction
- [x] Settings (pydantic-settings) and structured JSON logging

## Milestone 1: Vertical slice -- DONE

- [x] Kubernetes client setup: in-cluster / local kubeconfig (`observer/client.py`)
- [x] Pod watch loop with list-then-watch, 410 Gone relist, exponential backoff reconnect,
      bounded resync timer (`observer/watchers.py`, `observer/backoff.py`)
- [x] Evidence extraction from Pod status/conditions/container states (`observer/inspector.py`)
- [x] Diagnostic rules: CrashLoopBackOff, OOMKilled (incl. inferred-from-exit-code-137
      fallback), ImagePullBackOff/ErrImagePull
- [x] Diagnostic engine: runs all rules, ranks hypotheses by confidence (`diagnostics/engine.py`)
- [x] Incident manager: dedup by resource UID, health classification, severity mapping,
      recovery detection (`incidents/`)
- [x] SQLite repository behind an abstract interface (`persistence/`)
- [x] FastAPI read endpoints: `/health /ready /incidents /incidents/{id}
      /incidents/{id}/evidence /incidents/{id}/actions /incidents/{id}/diagnose
      /cluster/summary /metrics`
- [x] CLI: `inspect`, `diagnose`, `incidents`, `evidence`, `audit`
- [x] Prometheus metrics wired for everything implemented so far (counters/gauges/histograms)
- [x] kind lab: namespace + 4 scenarios (crashloop, oom, imagepull, transient-recovery)
- [x] 40 unit tests (rules, health classification, dedup, SQLite roundtrip, backoff math,
      observer deletion-timestamp handling) -- `./scripts/test.sh`, all passing
- [x] Verified live end-to-end against a real kind cluster: all 3 permanent failure
      scenarios detected and correctly diagnosed; the transient-recovery scenario opened
      *and resolved* an incident with a real measured `recovery_duration_seconds`,
      confirmed via the API, CLI, and `/metrics`
- [x] ruff + mypy clean

## Milestone 2: Remaining diagnostic rules -- DONE (13 of 14; DNS deliberately deferred)

- [x] `MissingConfigReferenceRule` -- `CreateContainerConfigError`/`CreateContainerError`
      waiting reasons (no new watch needed -- already in Pod evidence)
- [x] `PendingSchedulingRule` -- classifies the Pod's own `PodScheduled=False/Unschedulable`
      condition message into `PENDING_INSUFFICIENT_RESOURCES` or
      `PENDING_SCHEDULING_CONSTRAINT` (taint/affinity), with an honest MEDIUM-confidence
      fallback when the message doesn't match a known pattern
- [x] `ReadinessProbeFailingRule` / `LivenessProbeFailingRule` -- required wiring up the
      `EventWatcher` that Milestone 1 had built but never activated; readiness failures in
      particular have **no** container-state signal at all, only the Warning Event
- [x] `RolloutFailedRule` / `UnsatisfiedReplicasRule` -- new `DeploymentWatcher`; Deployment-
      kind incidents, proving IncidentManager's UID-based dedup generalizes to any resource
      kind, not just Pods
- [x] `PvcBindingFailureRule` -- new `PVCWatcher` + Event correlation; deliberately does NOT
      treat PVC `Pending` phase alone as a failure (this project's kind cluster uses
      `WaitForFirstConsumer`, where Pending-with-no-consumer is normal)
- [x] `NodeConditionRule` -- new `NodeWatcher` (cluster-scoped, needs a ClusterRole --
      see `rbac/clusterrole.yaml`); NotReady/pressure conditions open an incident,
      cordoning alone does not (too often deliberate maintenance)
- [x] `ServiceSelectorMismatchRule` -- new `EndpointSliceWatcher`; fires only on *zero*
      endpoints (not zero-*ready*), to avoid misdiagnosing a Pod readiness problem as a
      Service problem
- [ ] DNS/connectivity (#11 in the original spec) -- **deliberately deferred**, not
      forgotten: the spec itself qualifies this one with "when the available evidence
      supports a diagnosis," unlike the other 13. Passive evidence alone (no active probe,
      which is out of scope) gives very weak DNS signal; building a rule anyway just to
      check a box would mean fabricating confidence the evidence doesn't support -- exactly
      what this project is trying not to do.
- [x] Refactored three near-duplicate watcher implementations (Pod/Event, then
      Deployment) into one shared `_ListWatchLoop` before adding a fourth and fifth
      (PVC, Node) -- see ARCHITECTURE.md
- [x] 77 new unit tests (119 total) -- `./scripts/test.sh`, all passing; ruff + mypy clean
- [x] Verified live against the real kind cluster for every new rule: cordon/uncordon
      for Node, a bad StorageClass for PVC, a non-matching Service selector, and the
      pre-existing Deployments' real `Available=False`/`Progressing` conditions
- [x] Found and fixed two more real bugs during live verification (see Known limitations
      #7 and #8) and one upstream client library bug (see #9)
- [x] RBAC (`rbac/role.yaml`, new `rbac/clusterrole.yaml` + `clusterrolebinding.yaml`)
      grown to match exactly the new resources watched

## Milestone 3: Policy engine -- DONE

- [x] `PolicyEngine.evaluate()` (`policy/engine.py`): a pure function, same shape as
      `DiagnosticEngine` -- no Kubernetes API, no DB, no clock reads of its own, every
      fact supplied by the caller in a `PolicyRequest`
- [x] Advisory action types (`RECOMMEND_*`) always `ALLOWED`, even in `OBSERVE`, since
      they never mutate cluster state
- [x] `OBSERVE` mode denies every mutating action type outright (not "proposes but
      never executes" -- denied before an action is even modeled as proposable)
- [x] Namespace allowlist (existing `action_allowed_namespace_list`) and a new,
      optional resource-name allowlist (`action_allowed_resource_name_list`, empty =
      no extra restriction)
- [x] Resource-type consistency check: `RESTART_DEPLOYMENT`/`ROLLBACK_DEPLOYMENT`/
      `SCALE_DEPLOYMENT` only apply to `Deployment` targets -- this project's reading
      of "whether the resource is managed by a controller"
- [x] Cooldown (per target + action type) and per-incident / per-window action budgets,
      using the config fields Milestone 1 had already scaffolded for this
- [x] `ROLLBACK_DEPLOYMENT`/`SCALE_DEPLOYMENT` always require approval, even in
      `CONTROLLED_AUTO` -- matches the spec's explicit "approval-gated scaling"
- [x] `CONTROLLED_AUTO` auto-allows only `RESTART_DEPLOYMENT`, and only when the
      incident is HIGH+ severity, the triggering hypothesis was HIGH confidence, *and*
      the target's desired replica count is known and > 1 (unknown is treated as
      "could be 1" -- conservative, not optimistic)
- [x] 27 new unit tests (147 total) -- `./scripts/test.sh`, all passing; ruff + mypy clean
- [x] Exercised against a **real incident pulled live from the cluster**
      (`crashloop-demo`'s Deployment-level `UNSATISFIED_REPLICAS` incident,
      `desired_replicas=1`) across all three modes -- confirmed the single-replica
      guard correctly blocks auto-execution even once severity reaches HIGH, and
      flips to `ALLOWED` once replica count is raised in the simulation. No
      remediation engine exists yet to make this an actual end-to-end cluster flow;
      see docs/REMEDIATION_SAFETY.md for the exact commands and output.
- [x] Found and fixed one more real bug while pulling that live incident data (see
      Known limitations #10) -- a second, more subtle instance of the same
      name-reuse problem #7 already covered: the existence check added for #7 only
      confirmed a same-*named* object existed, not the same object (by UID)

## Milestone 4: Remediation actions -- DONE

- [x] `remediation/executor.py::RemediationExecutor` -- the only code in this project
      permitted to mutate cluster state. Implements `RESTART_DEPLOYMENT` (patches a
      rollout-restart annotation, exactly what `kubectl rollout restart` does),
      `SCALE_DEPLOYMENT` (patches the scale subresource), `ROLLBACK_DEPLOYMENT` (reads
      ReplicaSet revision history via `deployment.kubernetes.io/revision` annotations,
      full-`replace`s the Deployment's `spec.template` with the target revision's),
      and the two `RECOMMEND_*` types (no cluster call at all)
- [x] Every mutating action re-reads the live Deployment first and verifies its UID
      still matches what the action was proposed against -- directly implements the
      spec's "the workload changes before the action executes" test case
- [x] Every API call carries a hard timeout (`action_execution_timeout_seconds`); every
      failure path is caught and recorded on the action (`execution_result` /
      `execution_error`), never raised to the caller
- [x] `remediation/service.py::RemediationService` -- the first code that actually
      calls `PolicyEngine.evaluate()` with live data: `propose()` builds an action,
      evaluates it, and executes immediately if `ALLOWED`; `approve()`/`reject()`
      handle the human-in-the-loop path for `REQUIRES_APPROVAL` actions
- [x] `remediation/recommendations.py` -- turns an OOM/ImagePull/config/probe/PVC/
      Service hypothesis into `RECOMMEND_*` action text, reusing the rule's own
      `explanation` + `recommended_next_steps` verbatim rather than inventing new advice
- [x] Fixed a real gap in Milestone 3's policy engine while building this: it never
      checked the diagnosing hypothesis's own `auto_remediation_permitted` flag, so
      `CONTROLLED_AUTO` could in theory have auto-allowed a restart a rule explicitly
      said wouldn't help. Added `PolicyRequest.hypothesis_auto_remediation_permitted`
      and gated `_auto_execution_eligible` on it.
- [x] `rbac/remediation-role.yaml` + `remediation-rolebinding.yaml` -- the first write
      permissions anywhere in this project, in a Role kept separate from the read-only
      observer Role so "what can read" and "what can change" stay independently
      auditable. Scoped to `patch`/`get`/`list`/`watch` on `deployments` (+ the `scale`
      subresource) and read-only `get`/`list` on `replicasets` -- no `delete` anywhere.
- [x] New lab fixture `lab/manifests/remediation-test/deployment.yaml` -- a healthy,
      stable, 2-replica Deployment that exists only to exercise remediation actions
      safely, deliberately separate from the broken failure-scenario Deployments
- [x] 182 total unit tests, all passing; ruff + mypy clean
- [x] **Verified live against the real kind cluster**, not just unit-tested: scaled
      `remediation-test` 2->3 (confirmed via a live GET that `spec.replicas` actually
      changed), restarted it (confirmed a new `resourceVersion` and a new ReplicaSet),
      and rolled it back to its original revision (confirmed the restart annotation
      was actually gone from the live Deployment and the original ReplicaSet was
      scaled back up) -- each verified against the cluster's own state, not just the
      action's own reported "success"
- [x] **Found and fixed three real bugs during that live verification**, each
      confirmed both via a unit test and a live re-run after the fix (see Known
      limitations #11-13): a `.to_dict()` serialization bug that would have sent
      wrong-cased keys and explicit `null`s in a merge patch (caught before it ever
      ran against the cluster); rejected actions wrongly counting against
      cooldowns/budgets; and a merge-PATCH-based rollback that silently failed to
      clear a field the newer revision had added, fixed by switching to a full
      `replace`
- [x] **Honest demonstration**: proposed a restart against imagepull-demo's real
      `ROLLOUT_FAILED` incident (HIGH severity, HIGH confidence) in `CONTROLLED_AUTO`
      mode. Confirmed it correctly stayed `PENDING_APPROVAL` rather than auto-executing,
      because the diagnosing rule's own `auto_remediation_permitted=False` says restart
      won't help. A human-approved override then executed the restart anyway, and the
      pod was confirmed still `ImagePullBackOff` afterward -- exactly as the diagnosis
      predicted. This is the spec's "a restart is not a universal fix" principle shown
      actually happening, not just asserted in a doc.

## Milestone 5: Recovery verifier -- DONE

- [x] `verification/verifier.py::RecoveryVerifier` -- polls the target Deployment
      with a deadline (`KUBESENTINEL_VERIFICATION_DEADLINE_SECONDS`, default 60s) and
      interval (`KUBESENTINEL_VERIFICATION_POLL_INTERVAL_SECONDS`, default 2s), never
      waiting indefinitely, and returns one of three outcomes --
      `RECOVERED`/`FAILED`/`INCONCLUSIVE` -- never collapsing "ran out of time with no
      clear signal" into either of the other two
- [x] Deliberately reuses Milestone 1-2's own `deployment_evidence`,
      `has_failure_signal`, `is_confirmed_healthy` rather than inventing a third
      definition of "is this Deployment healthy" -- the verifier's own job is just
      polling-with-a-deadline and picking the outcome, not redefining health
- [x] `models/action.py::VerificationOutcome` changed from a bare `recovered: bool`
      (unused Milestone 1 scaffolding) to a `VerificationStatus` enum
      (`RECOVERED`/`FAILED`/`INCONCLUSIVE`), with `.recovered` kept as a convenience
      property -- nothing else in the codebase depended on the old shape
- [x] Wired into `RemediationService._run()`: after `EXECUTED`, transitions to
      `VERIFYING`, calls the verifier, and sets the final terminal status
      (`VERIFIED_RECOVERED`/`VERIFICATION_FAILED`/`VERIFICATION_INCONCLUSIVE`) based
      on the outcome -- an action that failed to even execute skips verification
      entirely, per the spec's "never mark anything resolved on an action that
      didn't succeed"
- [x] `kubesentinel_recovery_duration_seconds` (defined in Milestone 1, unused until
      now) is recorded on a confirmed `RECOVERED` outcome, measuring
      executed-to-verified time, not the action's own reported timing
- [x] 196 total unit tests, all passing; ruff + mypy clean
- [x] **Verified live** against `remediation-test`: a real restart, verified end to
      end with the project's real (non-test-shortened) 60s/2s deadline/poll settings
- [x] **Found and fixed a real bug during that live run** (see Known limitations
      #15): the first version reported `RECOVERED` within 0.1s of triggering a
      restart, because Kubernetes deliberately keeps old Pods serving during a
      rollout -- the Deployment's aggregate ready/available counts looked fully
      satisfied via the *old* ReplicaSet before the new one had even started.
      Re-verified live after the fix (requiring `updatedReplicas` to also catch up
      for restart/rollback specifically): took 11.5s, and the pod list confirmed the
      new ReplicaSet had genuinely replaced the old one before `RECOVERED` was
      reported this time.

## Milestone 6: Full API + CLI -- DONE

- [x] `POST /incidents/{id}/actions/plan` (with a `dry_run: bool` body field --
      previews the policy decision with zero side effects, no action created),
      `/{action_id}/approve`, `/{action_id}/execute`, and `/{action_id}/reject`
      (the last isn't in the spec's literal endpoint list but is the natural
      complement to approve, and the service already supported it)
- [x] `require_api_token` dependency on every mutating endpoint: an **unset**
      `KUBESENTINEL_API_TOKEN` returns `503` (disabled), not `200` -- "no token
      configured" is never treated as "no auth needed." A set-but-wrong token is
      `401`. Matches the spec's "do not expose an unauthenticated, unrestricted
      remediation endpoint" literally -- there is no code path where this surface
      is reachable without a token someone deliberately configured.
- [x] **Split `RemediationService.approve()` from a new `execute()`**: Milestone 4/5
      had `approve()` execute inline, collapsing two of the spec's three distinct
      operations into one. Refactored so `approve()` only marks `APPROVED` (a human
      can approve now, execute later, or never); `execute()` is the second, separate
      call that actually runs the executor+verifier. `CONTROLLED_AUTO`'s `ALLOWED`
      path is unaffected -- it still executes inline from `propose()`, since there's
      no human to wait for.
- [x] New `RemediationService.dry_run()`: builds a `PolicyRequest` and evaluates it
      against the *real* current action history (realistic cooldown/budget preview)
      but never builds, persists, or executes a `RemediationAction` -- true zero
      side effects, not just "doesn't call the executor."
- [x] CLI: `dry-run`, `plan`, `approve`, `execute`, `reject` -- all direct, like the
      existing CLI commands (construct the same service objects the API uses, no
      HTTP round-trip, work whether or not the API server is running)
- [x] 215 total unit tests (including a new `tests/unit/test_api.py` exercising the
      full plan/approve/execute/reject flow and the auth boundary via FastAPI's
      `TestClient`, bypassing the real-cluster `lifespan` by injecting state
      directly); ruff + mypy clean
- [x] **Verified live** both ways: via raw `curl` against the running API server
      (auth boundary: no token -> 401 here since a token *was* configured; wrong
      token -> 401; dry-run and real plan/reject against a genuine `crashloop-demo`
      incident) and via the CLI's `plan`/`approve`/`execute` against the real
      `remediation-test` Deployment (a real scale-down, confirmed against the
      cluster afterward)
- [x] Found and fixed two more real bugs during this live verification (see Known
      limitations #16 and #17)

## Milestone 7: Observability -- DONE

Metrics implemented: `kubesentinel_incidents_detected_total`, `_incidents_active`,
`_diagnostics_total`, `_diagnostic_duration_seconds`, `_recovery_duration_seconds`,
`_observer_errors_total` (wired into every watcher's exception handlers, labeled
by component -- `pod_watcher`, `event_watcher`, `deployment_watcher`, `pvc_watcher`,
`node_watcher`, `endpointslice_watcher`), plus remediation metrics
(`_remediation_attempts_total`, `_remediation_success_total`,
`_remediation_failures_total`, `_actions_requiring_approval`). All labels are a
small, bounded set of known values (category, action_type, reason, component) --
never incident IDs, pod names, or other unbounded strings.

Added `observability/prometheus.yml` (a minimal standalone scrape config -- this
project runs as a local process against the cluster via kubeconfig, not an
in-cluster workload, so there is no Service/Pod to discover; the one real target
is the FastAPI process's own `/metrics`) and `observability/grafana-dashboard.json`
(importable dashboard: active incidents by category, detection rate, diagnostic
duration p50/p95, recovery duration p50/p95 by category, remediation
attempts/successes/failures, actions requiring approval, observer errors by
component).

**Live-verified, not just written**: ran a throwaway `prom/prometheus` container
against the real demo server's `/metrics` and confirmed every PromQL expression
used by the dashboard's panels evaluates successfully against real exported
samples (`histogram_quantile` over the real duration buckets, `rate()` over the
real counters) -- not just that the JSON is well-formed. This process is also
what surfaced the real `incidents_active` reconciliation bug documented as Known
limitation #18 below; the dashboard's "active incidents" panel would otherwise
have quietly under-reported on every real restart.

## Milestone 8: Remaining lab scenarios -- DONE (9 of 9)

Built: crashloop, oom, imagepull, transient-recovery (Milestone 1), pvc-binding,
service-selector-mismatch (Milestone 2), and now pending-resources,
readiness-probe-failure, failed-rollout -- all verified live via their actual
manifest files, not just ad hoc `kubectl` commands. See FAILURE_LAB.md for each
scenario's exact mechanism and expected/confirmed behavior.

**Live-verified**: applied all three new manifests to the real kind cluster.
`pending-resources-demo`'s `PodScheduled` condition read exactly
`status=False, reason=Unschedulable, message="0/1 nodes are available: 1
Insufficient cpu, 1 Insufficient memory."`. `readiness-probe-failure-demo`
produced a real `Warning Unhealthy / Readiness probe failed` Event while staying
`Running` with `restartCount=0`. Both the real observer+incident-manager
pipeline (via the long-running demo API server) and the unit-tested rules
diagnosed `PENDING_INSUFFICIENT_RESOURCES` and `READINESS_PROBE_FAILING`
respectively, confirmed via `/incidents`.

Also found and fixed a real bug in my own first draft of the `failed-rollout`
manifest while verifying it live -- see Known limitation #19 below. After the
fix, `kubectl wait --for=jsonpath='{.status.conditions[?(@.type=="Progressing")].reason}'=ProgressDeadlineExceeded`
succeeded within the 15s `progressDeadlineSeconds`, and the real pipeline
correctly opened a `ROLLOUT_FAILED` incident on the Deployment itself (a
separate incident from the Pod-level ones the same Pods also produced).

Updated `scripts/demo.sh`'s `all` scenario loop and
`lab/scripts/run-scenario.sh`'s usage text (previously hardcoded to only the
first three scenarios ever built) to reflect all nine.

## Milestone 9: Integration/e2e tests -- DONE

Added `tests/integration/` (16 RBAC-boundary tests, plus watch-resync,
observer-live-detection x2, restart-recovery, and concurrent-remediation -- 21
tests) and `tests/e2e/` (the full propose/approve/execute/verify lifecycle
against the real `remediation-test` Deployment -- 1 test). All 22 run against
the real `kind-kubesentinel` cluster through `tests/conftest.py`'s shared
fixtures, never mocked, and every one skips (not fails) when its precondition
isn't met -- confirmed live by pointing `KUBECONFIG` at a nonexistent file:
all 22 skipped cleanly in 0.22s. Also added a fast mock-based regression test
for the concurrency fix below directly to `tests/unit/`, so a regression there
is caught by plain `./scripts/test.sh`, not only `--all`. `./scripts/test.sh
--all` now runs 240 tests total (218 unit + 22 integration/e2e); plain
`./scripts/test.sh` runs 218 (no cluster touched). Re-ran the full combined
240-test suite four consecutive times with no failures, after finding and
fixing the flake described below. See TESTING.md for what each file covers.

Three real bugs were found and fixed in the process of writing these tests
(not theoretical edge cases -- each confirmed failing against the pre-fix
code, then re-confirmed passing after the fix):

- **A genuine concurrency race in `RemediationService.propose()`'s per-incident
  budget** -- see Known limitation #20 below.
- **Two test-design timing traps specific to this long-running demo
  environment** that would have made otherwise-correct tests flaky --
  `crashloop-demo`'s exponentially-growing kubelet restart backoff, and
  kubelet/client-go's own event-spam rate limiter delaying a freshly recreated
  Pod's first `Unhealthy` Event by over a minute after many hours of
  identical repeated events. Neither is a KubeSentinel bug; both are
  documented in `tests/integration/conftest.py`'s fixture docstrings as the
  reason those specific tests target `pending-resources`/
  `readiness-probe-failure` instead of `crashloop`.
- **A race in one of the new tests itself, not the product**: initially
  misdiagnosed as a system-load timing issue (raising the poll timeout from
  20s to 45s to 90s didn't help -- the real failure happened almost
  immediately every time, not after waiting out a long timeout, which was the
  tell that it wasn't actually a timeout problem). Root cause:
  `test_observer_opens_real_incident_for_live_probe_failure_pod`'s
  `poll_until` predicate checked "is *any* `DIAGNOSED` incident open in the
  namespace yet," not specifically the one for the pod under test -- in the
  full combined suite, several other scenarios in `kubesentinel-lab` are also
  broken at once, each handled by an independent watcher thread, so the weak
  predicate could be satisfied by an unrelated scenario's incident before this
  Event-driven one had been processed, returning early with the wrong result.
  Fixed by polling specifically for an incident matching the exact pod name
  under test. Re-ran the full 240-test suite four times after the fix with no
  further failures (the timeout was put back down to 30s once the real bug,
  not the timeout, was the problem).

## Milestone 10: Documentation -- DONE

Every doc file brought up to date with the project's actual current scope
(Milestones 1-9), not left matching whatever milestone last touched it:

- **README.md**: status line corrected to "1-9 of 11," added lab-scenario and
  testing completeness, fixed two broken documented CLI commands (see #21).
- **ARCHITECTURE.md**: full rewrite -- the pipeline diagram only showed
  Milestone 1's observe/diagnose/persist/expose boxes; added the
  policy/remediation/verification/metrics layers, a new "why this shape"
  passage for each, and replaced the stale "planned additions" (describing
  Milestones 3-5 as not yet built) with what's genuinely still not built.
- **KUBERNETES_CONCEPTS.md**: expanded from a Milestone-1-only stub (explicitly
  said "Milestone 1 doesn't even watch Events yet," false since Milestone 2)
  to cover every concept this project actually exercises: probes, scheduler
  messages, rollout semantics (including both live-caught verifier bugs),
  PVC binding modes, Service/EndpointSlice mismatches, Node RBAC, PATCH vs.
  PUT verb semantics, and concurrency -- each tied to the specific code and
  the specific live finding, not a generic primer.
- **DESIGN_DECISIONS.md**: added the Milestones 3-9 decisions that were made
  but never written down (ordered policy checks, why `RECOMMEND_*` bypasses
  gating, why `REJECTED` excludes from budget/cooldown, why
  propose/approve/execute are three methods not two, why the verifier needs a
  stricter check than the passive incident manager, why `incidents_active`
  needs explicit reconciliation, why `propose()`'s lock is scoped narrowly).
- **RBAC_AND_THREAT_MODEL.md**: fixed two outright false claims ("the policy
  engine... not yet built" and "API authentication: not yet enforced," both
  false since Milestones 3 and 6 respectively); added a note on the
  Milestone 9 automated RBAC boundary tests.
- **docs/DEMO_SCRIPT.md**: replaced the false "no remediation is possible yet"
  closing section with a real, live-verified remediation walkthrough (step 8)
  against an actual diagnosed incident, plus all nine lab scenarios in the
  setup steps (previously only listed the first three ever built).
- **FAILURE_LAB.md, TESTING.md, TROUBLESHOOTING.md, DIAGNOSTIC_RULES.md**:
  already current from Milestones 7-9's own documentation passes; re-checked
  for staleness, none found beyond TROUBLESHOOTING.md's new entry (#21).

Re-ran `./scripts/test.sh` (218 passed) after all doc edits to confirm nothing
was inadvertently broken, since this milestone touched zero application code
except the TROUBLESHOOTING-documented CLI fix, which was a README correction,
not a code change.

## Milestone 11: Optional AI integration -- DONE

Added `src/kubesentinel/ai/` (`client.py::OllamaClient`/`AiClient` Protocol,
`summarizer.py::summarize_incident()`/`build_prompt()`) and
`models/ai.py::AiSummary`. The only backend is a local Ollama server --
genuinely free (no API key, no cloud account, no per-request cost), not a
"free tier." Disabled by default (`KUBESENTINEL_AI_ENABLED=false`); every
other feature in this project works fully without it.

Wired into both surfaces, read-only:

- `GET /incidents/{id}/ai-summary` (no auth token needed -- it's read-only
  and mutates nothing, same reasoning as the other GET endpoints; gated on
  `KUBESENTINEL_AI_ENABLED` instead, since disabling it is about availability/
  cost, not a security boundary). `503` if disabled, `502` if the backend is
  unreachable/errors, never a silently fabricated summary.
- `kubesentinel explain <incident-id>` (CLI, same direct-construction pattern
  as every other command).

**Why this can never become an authorization path, not just "won't" but
"structurally can't":** `summarize_incident()` takes an already-built
`Incident` and an `AiClient`; it has no reference to `ClusterClients`, the
repository, or `PolicyEngine` at all, so there is no code path from a model's
output back into a diagnosis, a policy decision, or an executed action.
`build_prompt()` only includes the same structured `Hypothesis`/
`RemediationAction` fields a human already sees via `kubesentinel evidence` --
never raw Pod/Event/annotation text -- so there is nothing in the prompt a
workload could use to try to inject instructions into the model. See
RBAC_AND_THREAT_MODEL.md and DESIGN_DECISIONS.md.

**Live-verified**, against a real local Ollama server (`qwen2.5-coder:3b`,
already installed): `tests/integration/test_ai_summary_live.py` (skips if
Ollama isn't reachable), plus manual verification of both surfaces --
`kubesentinel explain` against a real `READINESS_PROBE_FAILING` incident
returned an accurate, policy-respecting summary ("This issue needs to be
addressed..." -- notably, it did *not* say "I restarted it" or invent a
next step not already in the hypothesis); the `/ai-summary` HTTP endpoint
returned the same quality of result with `200`; disabling
`KUBESENTINEL_AI_ENABLED` correctly returned `503` on both surfaces.
16 new unit tests (`test_ai_client.py`, `test_ai_summarizer.py`, plus 4 in
`test_api.py`) mock the HTTP call entirely, so `./scripts/test.sh` (no
cluster, no Ollama) stays fast and fully offline -- 234 unit tests total.

---

# Resource Optimization Engine (extension)

A second, read-only engine: analyzes actual workload CPU/memory consumption
and produces evidence-based right-sizing recommendations, sharing
infrastructure with (but never tightly coupled to) the troubleshooting/
self-healing engine above. See
[docs/RESOURCE_OPTIMIZATION_DESIGN.md](docs/RESOURCE_OPTIMIZATION_DESIGN.md)
for the full design. Numbering continues from Milestone 11 above rather than
restarting, to keep one flat timeline.

## Milestone 12: Repository audit, design doc, baseline -- DONE

Audited the existing codebase against the extension spec (see the design
doc's §1 for the full table of what's reused vs. new). Key findings:

- No resource-optimization code exists anywhere yet.
- No Kubernetes quantity-string parser exists anywhere (confirmed via
  `grep`) -- net-new foundational work for Milestone 13.
- `kubernetes==30.1.0` has no typed Metrics API client; it must be queried
  via `CustomObjectsApi` against `metrics.k8s.io/v1beta1`, returning plain
  dicts -- `ClusterClients` needs a `custom_objects` field added (additive).
- `AppsV1Api` (already a dependency) covers Deployments/ReplicaSets/
  StatefulSets/DaemonSets; Jobs need `BatchV1Api`, not currently constructed
  anywhere.
- `inspector.py::resource_ref_from_pod` resolves only **one** level of
  ownership (Pod -> immediate owner) -- correct for incident identity, not
  sufficient for optimization's workload-level view. A new, separate
  `optimization/ownership.py` walks the full chain; `inspector.py` is left
  unmodified since its current behavior is still correct for what uses it.
- Live-confirmed on the lab kind cluster: `metrics-server` is **not
  installed** (`NotFound`), and no Prometheus is running. The engine must
  treat "no metrics" as a normal, honest first-class state from day one, not
  an edge case to patch in later -- this shaped the `EvidenceQuality` design
  in the doc's §5.
- Baseline test run before any optimization code: `./scripts/test.sh` --
  ruff clean, mypy clean, **234 unit tests passed**. (`--all` against the
  live cluster was not re-run for this baseline specifically because the
  dev kind cluster is currently sharing its host VM with an unrelated
  project and showing real resource contention -- see the existing
  "Environment note" below; this is noted so a slow live check during this
  extension isn't mistaken for a regression this extension introduced.)

No code was written this milestone, per the spec's own explicit instruction
not to attempt a rewrite before the design is reviewed.

## Milestone 13: Typed models, quantity utilities, metrics-provider interface -- DONE

Added, touching no existing troubleshooting code's behavior:

- `optimization/quantities.py` -- `parse_cpu_to_millicores()`/
  `parse_memory_to_bytes()`/`format_cpu_millicores()`/`format_memory_bytes()`.
  Uses `decimal.Decimal` throughout, never `float` -- Kubernetes' own
  quantity type is exact decimal arithmetic, and converting through a binary
  float anywhere in this pipeline would be a silent precision bug. Supports
  the full real-world suffix set, including the nano/micro-core forms
  metrics-server actually returns (e.g. `"132n"`), not just the request/
  limit forms (`"100m"`, `"0.5"`) a manifest typically uses.
- `models/optimization.py` -- `ResourceRecommendation`, `WorkloadSnapshot`,
  `ContainerResourceSnapshot`, `UsageSummary`, `ResourceQuantity`, and the
  `RecommendationCategory`/`EvidenceQuality`/`RecommendationStatus`/
  `WorkloadKind` enums. `RecommendationStatus` deliberately has no
  `APPLIED`/`FAILED` member yet -- there is no apply path in this phase (see
  RESOURCE_OPTIMIZATION_DESIGN.md §6); adding those statuses ahead of the
  code that would set them would be exactly the kind of premature
  scaffolding this project avoids elsewhere (see the RBAC principle in
  RBAC_AND_THREAT_MODEL.md: "grants grow alongside implemented capability,
  not ahead of it" -- the same philosophy applied to a model's own enum).
- `optimization/metrics_providers.py` -- the `MetricsProvider` Protocol
  (same shape as `ai/client.py::AiClient`), `MetricsApiProvider` (queries
  `metrics.k8s.io/v1beta1` via `CustomObjectsApi`, since the pinned
  `kubernetes==30.1.0` client has no typed client for this aggregated API --
  confirmed by reading the installed package), `PrometheusProvider`
  (optional, stdlib `urllib` only, same approach as `OllamaClient`), and
  `InMemorySampleStore` (a bounded per-container ring buffer -- this
  project's "poor man's history" when only the snapshot-only Metrics API is
  available).
- `observer/client.py::ClusterClients` extended with `custom_objects`
  (`CustomObjectsApi`) and `batch` (`BatchV1Api`) fields, both now
  constructed unconditionally in `build_clients()` -- purely additive;
  confirmed no existing caller of `.core`/`.apps`/`.discovery` needed to
  change (`grep`-confirmed `ClusterClients(...)` is only ever constructed in
  one place, so making the two new fields required rather than `Optional`
  was safe and simpler than threading `None`-checks through every future
  caller).
- `config.py` -- `optimization_metrics_timeout_seconds`,
  `optimization_prometheus_url` (empty = disabled, mirroring `ai_enabled`'s
  pattern), `optimization_prometheus_timeout_seconds`,
  `optimization_sample_store_max_samples`, `optimization_excluded_namespaces`
  (+ its `_list` property, matching every other comma-separated setting's
  convention).

**Live-verified**, not just unit-tested: ran `MetricsApiProvider` against
the real lab kind cluster (which, per Milestone 12's audit, genuinely has no
metrics-server installed) and confirmed it raises `MetricsProviderError`
with the real `404` from the API server -- the exact honest-failure
behavior the spec requires, not a mock standing in for it. Also confirmed
`build_clients()` still constructs all five clients correctly against the
live cluster after the `ClusterClients` change.

**Tests**: 60 new unit tests (`test_quantities.py`: 44, covering CPU/memory
parse+format+round-trip+rejection of malformed input;
`test_metrics_providers.py`: 16, covering both providers against mocked
HTTP/API responses plus the sample store's bounding behavior) -- all
offline, no cluster or Ollama needed. `./scripts/test.sh`: ruff clean, mypy
clean (58 source files), **294 unit tests passed** (234 baseline + 60 new).

## Milestone 14: Workload inventory and ownership resolution -- DONE

Added:

- `optimization/ownership.py::resolve_pod_owner()` -- resolves a Pod's full
  owner chain (Pod -> ReplicaSet -> Deployment, or Pod -> StatefulSet/
  DaemonSet/Job directly, or standalone). Prefers the `ownerReferences` entry
  marked `controller=True`; a ReplicaSet whose own owner isn't in the
  caller-supplied map, or a Pod owned by an unrecognized controller kind
  (a CRD-based operator, Argo Rollout, etc.), resolves to `None` rather than
  guessing -- the caller decides what to do with an unresolved Pod, never a
  silent "treat it as standalone" fallback that would misrepresent a
  perfectly well-managed workload as an orphan.
- `optimization/inventory.py::build_workload_inventory()` -- groups Pods by
  resolved owner into `WorkloadSnapshot`s, reading requests/limits from the
  **owning workload's own pod template**, never a live Pod's spec (the
  spec's "never edit an individual Pod when its resource configuration
  belongs to a controller template" principle applied to *reading*, not just
  writing: if the owning object itself wasn't found in this inventory pass,
  the workload is skipped with a warning rather than substituting a live
  Pod's spec as a stand-in for the template). Terminating Pods
  (`deletionTimestamp` set) are excluded, mirroring
  `observer/service.py::ObserverService`'s existing principle. Health is
  computed from Pod readiness, restart count, and OOMKilled history --
  deliberately independent of the troubleshooting engine's own incident
  thresholds (see RESOURCE_OPTIMIZATION_DESIGN.md §2 on the one-way
  dependency between the two engines).

**A real bug found and fixed via live verification against the actual kind
cluster** (not theoretical -- confirmed wrong before the fix, re-confirmed
correct after it), plus the read-only guarantee confirmed at the strongest
level this project has checked it yet:

- **`status.readyReplicas`/`status.numberReady` are `omitempty` in the
  Kubernetes API.** Running the real inventory builder against the lab's own
  genuinely-broken Deployments (`crashloop-demo`, `oom-demo`, etc.) showed
  `ready_replicas=None` for every one of them -- confirmed via `kubectl get
  deployment ... -o jsonpath='{.status}'` that the field is entirely
  *absent* from the real API response when its value is 0 (Go's `omitempty`
  on a zero-valued int), not present-and-zero. The generated client
  deserializes that absence as `None`, which this project's own
  `WorkloadSnapshot.ready_replicas` uses to mean "unknown" (correctly, for a
  Job, where the field doesn't apply at all) -- left uncorrected, every
  genuinely-zero-ready workload would have misreported as "ready replicas
  unknown" instead of the true, far more actionable "0 of N ready," right
  when an operator would most want to know that number. Fixed by
  normalizing `None -> 0` specifically for `ready_replicas`/`number_ready`
  in `_replica_counts()` (not for `desired_replicas`, which has no such
  `omitempty` behavior -- Kubernetes defaults `spec.replicas` to 1 and
  always includes it). Re-verified live: every broken Deployment in the lab
  now correctly reports `ready=0`, not `ready=None`.
- Live-verified the read-only guarantee at the actual HTTP transport layer,
  not just by code inspection: patched the real `urllib3` pool manager
  underneath the live cluster's own `CoreV1Api` client and ran a full
  inventory build against it, confirming only `GET` requests were ever
  issued. This is now a permanent, repeatable check
  (`tests/integration/test_optimization_inventory_live.py::
  test_inventory_never_issues_a_non_get_http_request`), not a one-off.

Also confirmed live: ran the real inventory builder against the full
`kubesentinel-lab` namespace and spot-checked results against
`kubectl` -- `pending-resources-demo`'s absurd 100-core/1000Gi request
parsed correctly (`cpu_millicores=100000`, `memory_bytes=1073741824000`),
`remediation-test` correctly resolved as `healthy=True, desired=2, ready=2`,
and `failed-rollout-demo`/`imagepull-demo` each correctly collapsed two
ReplicaSet generations' Pods into one Deployment-level workload (not two)
-- exactly the "several replicas belong to one workload" requirement, shown
working across a real rollout, not just in a unit test.

`./scripts/test.sh --all`'s combined run showed 6 unrelated failures
(RBAC-boundary/observer-detection/restart-recovery tests, none of which this
milestone touched) while the shared Colima VM was again under the same
resource contention from an unrelated project documented in this file's
"Environment note" (below) -- re-ran all 6 in isolation immediately
afterward and every one passed, confirming environment noise, not a
regression.

**Tests**: 21 new unit tests (`test_ownership.py`: 10, `test_inventory.py`:
11) plus 2 new live integration tests (`test_optimization_inventory_live.py`,
gated on cluster reachability like every other test in that directory).
`./scripts/test.sh`: ruff clean, mypy clean, **315 unit tests passed** (294
baseline + 21 new).

## Milestone 15: Deterministic CPU/memory analysis and safety checks -- DONE

Added:

- `optimization/analyzer.py` -- the one public entry point,
  `analyze_workload()`. Pools raw usage samples across every Pod currently
  backing a workload (never per-replica -- a recommendation targets the
  shared container template, so "typical/peak demand" must mean demand
  across the whole workload), records them into `InMemorySampleStore` for
  next time, and optionally upgrades to a real Prometheus range query
  (`container_cpu_usage_seconds_total`/`container_memory_working_set_bytes`,
  the cAdvisor-standard metric names) when `KUBESENTINEL_OPTIMIZATION_PROMETHEUS_URL`
  is set -- any Prometheus failure (unreachable, no data, ambiguous query)
  is caught and falls back to the snapshot/in-memory evidence, never raises.
  CPU proposals are based on a configurable *percentile* of observed demand
  (default p95, not the bare average); memory proposals are based on
  observed *peak* (default p99), never the average -- the spec's explicit
  CPU-vs-memory distinction, enforced structurally by using different
  source statistics, not just differently-worded reasoning text. Nothing
  below `LIMITED_HISTORY` evidence quality (at least 3 pooled samples
  spanning at least 5 minutes, both configurable) is ever allowed to
  produce a `proposed_requests` number -- "do not recommend aggressive
  downsizing from a single sample," enforced in code, not just as a
  principle in this document.
- `optimization/safety.py::apply_safety_checks()` -- flags (never silently
  drops) unhealthy workloads, system namespaces, and Jobs/DaemonSets
  (`UNHEALTHY_WORKLOAD`/`SYSTEM_NAMESPACE`/`JOB_CONSERVATIVE`/
  `DAEMONSET_CONSERVATIVE`), suppressing only `proposed_requests`/
  `proposed_limits` on a flagged row -- the row itself, its `reasoning`, and
  every other safety flag survive, so "KubeSentinel looked at this and chose
  not to propose a number" stays visibly different from "KubeSentinel never
  looked at this at all."
- A historical OOM kill (`ContainerResourceSnapshot.recent_oom_detected`,
  set by `inventory.py`) is surfaced as its own `OOM_HISTORY`-flagged
  `MEMORY` recommendation *even with zero usage evidence* -- the OOM event
  itself is the evidence -- and specifically vetoes any proposed *decrease*
  to that container's memory request regardless of how quiet a recent
  window looks, per the spec's "a historical OOM kill should increase
  scrutiny rather than automatically trigger a larger limit" (this project
  reads that as cutting both ways: never silently waved away either).
- `WorkloadSnapshot.unschedulable_reason` (set by `inventory.py` from a real
  `PodScheduled=False/Unschedulable` condition) feeds a `SCHEDULING`
  category recommendation -- but only when the scheduler's own message
  classifies as `PENDING_INSUFFICIENT_RESOURCES`; a taint/affinity-caused
  Pending is correctly left alone as not a resource-sizing concern. This
  reuses `diagnostics/rules/scheduling.py`'s existing message
  classification *by import*, now exposed as the public
  `classify_unschedulable_message()` (renamed from the module-private
  `_classify`, its one other call site updated) -- exactly the "reusing
  existing collection infrastructure, not a copy" the design doc committed
  to, not a second, possibly-diverging copy of the same pattern-matching
  logic.
- `config.py` -- the analysis thresholds this milestone's logic actually
  reads (`optimization_min_samples_for_history`,
  `optimization_min_window_seconds_for_history`,
  `optimization_cpu_typical_percentile`, `optimization_cpu_safety_margin`,
  `optimization_cpu_floor_millicores`, `optimization_memory_peak_percentile`,
  `optimization_memory_safety_margin`, `optimization_memory_floor_bytes`,
  `optimization_underutilization_ratio`, `optimization_near_limit_ratio`,
  `optimization_recommendation_ttl_seconds`) -- added alongside the code
  that uses each one, not ahead of it.

Deliberately deferred to Milestone 16 (per RESOURCE_OPTIMIZATION_DESIGN.md's
own split): cluster-level overcommitment analysis (needs Node capacity
aggregated across every workload, which doesn't fit the per-container
`ResourceRecommendation` shape -- it belongs in the `/optimization/summary`
endpoint this milestone doesn't build yet) and the `ACTIVE_INCIDENT` safety
flag (needs `IncidentRepository`, which `safety.py` deliberately doesn't
depend on yet -- added ahead of the integration work that would set it would
be exactly the premature-scaffolding this project avoids elsewhere).

**Live-verified against the real cluster**, not just unit-tested: ran
`analyze_workload()` against the real inventory of the actual
`kubesentinel-lab` namespace (no metrics-server installed, so every
usage-based CPU/memory check correctly stayed silent) and confirmed it still
surfaced exactly the two workloads that should be flagged from
non-usage evidence alone:
- `oom-demo`: a real recorded `OOMKilled` termination -> `MEMORY` category,
  `OOM_HISTORY` + `UNHEALTHY_WORKLOAD` flags, no proposed number.
- `pending-resources-demo`: the real scheduler message ("0/1 nodes are
  available: 1 Insufficient cpu, 1 Insufficient memory.") -> `SCHEDULING`
  category, `UNSCHEDULABLE` + `UNHEALTHY_WORKLOAD` flags.

Every other real workload in the namespace (fully-specified resources,
healthy, schedulable, just genuinely lacking usage evidence without
metrics-server) correctly produced **zero** recommendations -- confirming
the "quiet when there's nothing actionable to say" behavior works against
real data, not only in a unit test's hand-built fixture.

**Tests**: 35 new unit tests (`test_analyzer.py`: 22, including a real
mocked-HTTP Prometheus upgrade path and its failure-fallback;
`test_safety.py`: 8; 5 more added to `test_inventory.py` for the new
`recent_oom_detected`/`unschedulable_reason` extraction, 16 total there
now). `./scripts/test.sh`: ruff clean, mypy clean, **350 unit tests
passed** (315 Milestone 14 baseline + 35 new).

---

## Milestone 16: API and persistence integration -- DONE

Added:

- `persistence/optimization_repo.py::SQLiteOptimizationRepository` -- a new
  `OptimizationRepository` ABC (`persistence/interface.py`) implemented
  against the *same* `db_path` the troubleshooting engine already uses, in
  3 new tables (`optimization_recommendations`,
  `optimization_workload_snapshots`, `optimization_analysis_runs`) -- reusing
  the existing SQLite file and connection-per-call/lock pattern from
  `SQLiteIncidentRepository`, not a second database. The analysis-run summary
  uses a fixed `_LATEST_ANALYSIS_RUN_ID = "latest"` single-row
  upsert -- `GET /optimization/summary` never needs to scan the cluster or
  recompute anything to answer "when did analysis last run and what did it
  see," it just reads one row.
- `api/optimization_routes.py` -- a new `APIRouter` (`/optimization/...`)
  with `POST /analyze` (the *only* route that touches the cluster -- every
  other route below serves already-persisted data from the last `analyze()`
  call), `GET /summary`, `GET /workloads`, `GET /recommendations` (with
  `namespace`/`workload`/`category`/`status` filters), `GET
  /recommendations/{id}` (404 via `RecommendationNotFoundError`), and `POST
  /recommendations/{id}/review` (token-gated via the *existing*
  `require_api_token` dependency -- the same gate the troubleshooting
  engine's plan/approve/execute/reject already use -- 404/409 via
  `RecommendationNotFoundError`/`InvalidRecommendationStateError`).
  Deliberately **no apply/execute endpoint**: there is no code anywhere in
  this project that patches a workload's resources, so exposing a route that
  implied otherwise would be exactly the misleading-endpoint-for-
  unimplemented-behavior the spec warns against (see
  RESOURCE_OPTIMIZATION_DESIGN.md §6).
- `api/state.py` (new) -- `AppState`, the module-level `state`, and
  `require_api_token` moved out of `app.py` into their own module so both
  `app.py` and `optimization_routes.py` can depend on the same state/auth
  dependency without a circular import (`app.py` is what imports
  `optimization_routes`, so `optimization_routes` can't import from
  `app.py`). No behavior change to either -- a pure extraction.
- `optimization/service.py::OptimizationService` -- the layer the routes and
  CLI both call through: `analyze()` (collects inventory + raw samples,
  runs `analyze_workload()` per workload, persists the run and its
  recommendations), `get_recommendation()`/`list_recommendations()`/
  `list_workloads()`/`summary()`, and `review()`. Two correctness properties
  enforced here, not in the storage layer: **effective status is computed
  at read time, never stored** -- `_with_effective_status()` returns a
  `model_copy()` with `status=EXPIRED` once `expires_at` has passed, so
  reading a stale recommendation never itself mutates the row (no background
  expiry job exists, by design); and **supersession never clobbers a human
  decision** -- each `analyze()` run expires any *prior* same-
  (namespace, workload, container, category) recommendation still sitting in
  `NEW`, but anything already `REVIEWED`/`APPROVED`/`REJECTED` is left
  completely untouched by later runs.
- CLI (`cli/main.py`): `optimize`, `recommendations`, `recommendation`,
  `optimization-summary`, `review-recommendation` -- same
  `_build_optimization_service()` construction pattern as the existing
  commands, table output via Rich / `--json` via plain `print()`.

**Bug found and fixed this milestone**: every one of the 15 pre-existing
`console.print(json_module.dumps(...))` call sites across the *entire* CLI
(every `--json` flag, old and new commands alike, dating back to Milestone 6)
was silently corrupting its own JSON output. Found live running
`kubesentinel recommendation <id> --json`: a long `reasoning` string got a
literal newline inserted mid-string by Rich's terminal-width word-wrapping,
and the output failed to parse (`json.decoder.JSONDecodeError: Invalid
control character`). Root cause: Rich's `console.print()` always word-wraps
at terminal width regardless of content type, and no prior JSON field in this
project had ever been long enough to visibly wrap -- so the bug existed,
undetected, since the very first `--json` flag was added. Fixed by replacing
every `console.print(json_module.dumps(...))` with the builtin
`print(json_module.dumps(...))` (`console.print()` is still used for every
human-facing table/text output -- only the JSON paths changed). Re-verified
live: the same command's `--json` output now parses correctly.

**Live-verified against the real cluster and a running API server**, not
just unit-tested:
- CLI: `optimize --namespace kubesentinel-lab`, `recommendations`,
  `recommendation <id>` (both table and `--json`), `optimization-summary`,
  and `review-recommendation <id> APPROVED --reviewed-by ... --notes ...`
  all run against the real lab, producing the same `oom-demo`/
  `pending-resources-demo` recommendations Milestone 15 found, now
  persisted and reviewable.
- HTTP API: started the real FastAPI app (`uvicorn ... --app-dir src`)
  against a real kubeconfig pointed at `kind-kubesentinel`, with
  `KUBESENTINEL_API_TOKEN` set. Confirmed live:
  - `POST /optimization/analyze` (both with and without a namespace filter)
    returns an honest `metrics_available: false` with the real
    `metrics.k8s.io pods query failed (status=404): Not Found` warning (no
    metrics-server installed on this lab cluster -- the "honest absence"
    path, not a crash).
  - `GET /optimization/summary` correctly aggregates by category/status
    across recommendations created by both the CLI session and the HTTP
    session against the same persisted data.
  - `GET /optimization/recommendations` and `GET /optimization/workloads`
    return the expected rows with correct filters.
  - `GET /optimization/recommendations/{id}` returns the full typed
    recommendation body for a real ID.
  - `POST /optimization/recommendations/{id}/review`: confirmed `401`
    with no `Authorization` header and a real `200` with
    `Authorization: Bearer <token>`, the response showing the persisted
    `status`/`reviewed_by`/`reviewed_at`/`review_notes` -- the same
    token-gating behavior the troubleshooting engine's mutating endpoints
    already have, now proven for this engine's own write path too.

**Tests**: 42 new unit tests (`test_optimization_repo.py`: 13;
`test_optimization_service.py`: 16; `test_optimization_api.py`: 13, after
fixing two initial failures -- a missing `_for_all_namespaces` mock set in
the shared fixture, and a mis-asserted 503-vs-401 case on the review
endpoint, corrected and renamed to `test_review_without_authorization_header_is_401`
once it was clear the fixture's own `api_token="test-token"` makes a missing
header a 401 case, not the "disabled entirely" 503 case already covered by
`test_api.py`). `./scripts/test.sh`: ruff clean, mypy clean, **392 unit
tests passed** (350 Milestone 15 baseline + 42 new). `./scripts/test.sh
--all` (full suite against the live cluster): **415 passed, 2 failed** --
both failures (`test_observer_still_detects_after_pod_deleted_and_recreated_with_same_name`,
`test_pod_watcher_relists_on_resync_boundary`) are pre-existing watch-stream
timing tests in the *troubleshooting* engine, untouched by this milestone's
changes. Confirmed as the same shared-VM contention already documented
below (not a regression): `docker stats` showed the unrelated
`ai-platform-engineer-control-plane` container at 263% CPU / 46% memory on
the same Colima VM at the time of the failure, and re-running both tests in
isolation immediately afterward passed cleanly.

## Milestone 17: Incident integration, Prometheus metrics, RBAC -- DONE

Added:

- `OptimizationService.__init__` takes an optional `incident_repo:
  IncidentRepository | None`. `analyze()` now computes, per workload, the
  currently-open (non-resolved) incident IDs via the *existing*
  `IncidentRepository.find_open_by_resource_uid` -- checked against the
  workload's own UID and every one of its current Pods' UIDs (deduped
  before querying, since a standalone Pod's `workload_uid` is the same
  value as its one entry in `pod_uids`), because an incident in this
  project can be opened against either, depending on which diagnostic rule
  found it (a Pod-level CrashLoopBackOff vs. a Deployment-level
  UNSATISFIED_REPLICAS). The result threads through
  `analyzer.py::analyze_workload`'s new `related_incident_ids` parameter
  into `safety.py::apply_safety_checks`, which now adds an `ACTIVE_INCIDENT`
  flag, suppresses the proposed number, and sets
  `ResourceRecommendation.related_incident_ids` -- `safety.py` itself still
  never touches a repository or the Kubernetes API directly, staying a pure
  function over already-fetched facts; the repository read happens once, in
  `service.py`, the one place in this engine that's allowed to have side
  effects. `app.py` wires `state.repo` (the troubleshooting engine's own
  `IncidentRepository` instance) straight into `OptimizationService`'s
  construction -- no second incident store, no duplicated incident concept.
  The parameter is optional and defaults to `None` (-> `[]`, never a crash
  or a false negative) specifically so every pre-existing test and CLI call
  site that doesn't pass one keeps working unchanged.
- `metrics/registry.py`: 4 new bounded-cardinality Prometheus metrics --
  `kubesentinel_optimization_analysis_total{status}` (success/partial/
  failed), `kubesentinel_optimization_recommendations_total{category}`,
  `kubesentinel_optimization_last_success_timestamp` (no labels),
  `kubesentinel_optimization_metrics_data_available{provider}`
  (metrics_api/prometheus). `OptimizationService.analyze()` now wraps its
  body in a `try`/`except Exception` that records `status="failed"` and
  re-raises unchanged (no new error-handling behavior, just honest
  observability of the existing one) on any unexpected failure (e.g. the
  cluster being unreachable); on a completed run it records
  `success`/`partial` based on `metrics_available`, increments
  `recommendations_total` by category, and sets
  `last_success_timestamp` -- deliberately on *both* `success` and
  `partial` outcomes, never on `failed`, since "did analyze() complete
  honestly" and "was usage data available" are different questions (the
  metric's own HELP text says so explicitly, to avoid a future reader
  assuming "last success" means "metrics worked"). The `prometheus`
  provider gauge reflects whether `KUBESENTINEL_OPTIMIZATION_PROMETHEUS_URL`
  is *configured*, not whether a query actually succeeded this run (the
  Prometheus upgrade happens per-container inside `analyzer.py`, not once
  per `analyze()` call, so there's no single run-level success/failure
  signal to report honestly without a larger refactor this milestone
  doesn't need) -- documented as exactly that in the metric's HELP string
  rather than overclaiming.
- `rbac/optimization-role.yaml` + `rbac/optimization-rolebinding.yaml`: a
  new, separate, read-only Role (`kubesentinel-optimizer`) for
  StatefulSets/DaemonSets/Jobs/ReplicaSets (`apps`/`batch`, `get`/`list`)
  and `metrics.k8s.io` Pods -- kept separate from `kubesentinel-observer`
  and `kubesentinel-remediator` for the same independent-auditability reason
  those two already are. Verbs are exactly `get`/`list`, never `watch`:
  confirmed by reading `inventory.py`, which takes a one-shot inventory
  snapshot per `analyze()` call and has no watch loop, unlike the
  troubleshooting engine's continuous Pod watch. No ClusterRole added for
  `metrics.k8s.io` Nodes: `MetricsApiProvider.get_current_node_usage()` has
  existed since Milestone 13 but has no caller anywhere in this codebase
  yet -- granting RBAC for a capability nothing uses yet would be exactly
  the "ahead of the code that uses it" the project's own RBAC convention
  (see `role.yaml`'s docstring) warns against.
- 15 new unit tests: 3 in `test_safety.py` (`ACTIVE_INCIDENT` flagging,
  multiple incident IDs recorded, no flag when the list is empty), 1 in
  `test_analyzer.py` (the parameter threads end-to-end through
  `analyze_workload`), 11 in `test_optimization_service.py`
  (`TestIncidentIntegration`: no-repo-wired-up is a no-op, a real match on a
  Pod UID flags and links correctly, a resolved incident never counts as
  active, each UID is checked exactly once; `TestOptimizationMetrics`: all
  4 metrics, using the same reset-then-assert `._value.get()` pattern
  already established for `incidents_active` in `test_incident_manager.py`).

**Deliberately not built this milestone**: cluster-level overcommitment
analysis (summing Node allocatable capacity against total requested across
every workload). This was floated as a "deferred to later" idea in
Milestone 15's own writeup, but re-reading RESOURCE_OPTIMIZATION_DESIGN.md
§13's actual Milestone 6 scope before starting this milestone's work showed
it was never actually committed to there -- only incident integration,
metrics, and RBAC were. Building it anyway would have been scope creep
beyond what the design doc's own plan promises; left as a possible future
addition, not a known gap in what this milestone claims to do.

**Live-verified against the real cluster and a running API server**, not
just unit-tested:
- `kubesentinel optimize --namespace kubesentinel-lab` against the real
  `kubesentinel.db` (which already has real, non-resolved incidents from
  the troubleshooting engine's own ongoing detection) correctly produced
  `ACTIVE_INCIDENT` on both expected recommendations: `oom-demo`'s `MEMORY`
  recommendation linked to *two* real incident IDs (one opened on the
  Deployment's own UID, one on a specific Pod's UID -- confirmed by reading
  both incidents directly out of the SQLite file), and
  `pending-resources-demo`'s `SCHEDULING` recommendation linked to the real
  incident opened on that Deployment's UID. Both had their proposed values
  correctly suppressed.
- Started the real FastAPI app against a throwaway db, `POST
  /optimization/analyze`, then scraped the real `/metrics` endpoint:
  confirmed `kubesentinel_optimization_analysis_total{status="partial"}`
  incremented on the clean run (no metrics-server on this lab cluster),
  `recommendations_total{category="MEMORY"}`/`{category="SCHEDULING"}` each
  at 1 (matching the 2 real recommendations), `last_success_timestamp` set
  to the partial run's own timestamp, and both
  `metrics_data_available{provider=...}` gauges correctly at 0 (no
  metrics-server, no Prometheus URL configured).
- An incidental but genuine confirmation of the `status="failed"` path: a
  transient cluster connectivity failure (the documented shared-VM
  contention -- see below) made one `/optimization/analyze` call raise for
  real; `/metrics` immediately afterward showed
  `kubesentinel_optimization_analysis_total{status="failed"}=1`, and a
  clean retry correctly left that counter alone while incrementing
  `{status="partial"}` instead -- exactly the distinct-and-correctly-
  separate behavior this metric was built to have, confirmed under a real
  failure rather than only a mocked one.

**Tests**: `./scripts/test.sh`: ruff clean, mypy clean, **407 unit tests
passed** (392 Milestone 16 baseline + 15 new). `./scripts/test.sh --all`:
**429 passed, 3 failed** -- all 3 failures
(`test_observer_opens_real_incident_for_live_probe_failure_pod`,
`test_observer_still_detects_after_pod_deleted_and_recreated_with_same_name`,
`test_pod_watcher_relists_on_resync_boundary`) are pre-existing
troubleshooting-engine tests that don't import or touch anything this
milestone changed (confirmed by reading their imports directly), and all 3
passed cleanly when re-run in isolation immediately afterward. `docker
stats` showed the same unrelated `ai-platform-engineer-control-plane`
container at 250%+ CPU on the shared Colima VM during the failing run,
consistent with the contention already documented after Milestones 9, 11,
and 16 -- not a regression.

## Milestone 18: Tests and live kind lab scenarios -- DONE

Most of this milestone's unit/integration test coverage had already been
added alongside each capability as it was built (Milestones 13-17 each
report their own new-test counts in their own sections above) --
per-milestone testing, not a separate catch-up pass, has been this
project's discipline since Milestone 1. What remained genuinely new here:
the live kind-cluster scenarios themselves (none of the existing lab
manifests exercise resource right-sizing), and two real bugs the testing
work surfaced.

Added:

- `lab/install-metrics-server.sh` -- a new, explicit opt-in script (not
  folded into `create-cluster.sh`, which the troubleshooting engine and its
  own scenarios need nothing from) that installs and patches metrics-server
  for kind, per RESOURCE_OPTIMIZATION_DESIGN.md Section 9's own commands.
  Installed live on this project's own kind cluster for the first time this
  session -- every prior milestone's live verification had correctly
  treated metrics-server's absence as the honest-degradation path to test;
  this milestone adds the complementary "it's present and working" path.
- Two new lab scenarios: `scenario-oversized-resources` (a Deployment
  requesting 1000m CPU / 256Mi memory for a container that only ever
  `sleep`s -- real usage reads as approximately zero) and
  `scenario-missing-requests` (a container with no `resources` block at
  all). Both labeled `kubesentinel.io/scenario` like every other scenario,
  so `lab/scripts/cleanup.sh`'s existing generic selector picks them up for
  free -- no change needed there.
- `tests/integration/test_optimization_live_scenarios.py` -- 3 new live
  tests: `missing-requests-demo` produces `DATA_QUALITY` with no proposed
  numbers from a single real pass; `oversized-resources-demo`'s real
  metrics-server sample reads as near-zero and a single pass still
  correctly withholds a proposed number (the "no aggressive downsizing
  from one sample" guarantee, now checked against real, not mocked, usage
  data for the first time in this project); and the read-only guarantee
  extended from `test_optimization_inventory_live.py`'s inventory-only
  check to the *entire* `OptimizationService.analyze()` path (inventory +
  metrics-server call + persistence) -- the specific check
  RESOURCE_OPTIMIZATION_DESIGN.md Section 12 asks for by name.
- `rbac/optimization-role.yaml` (written in Milestone 17) applied to the
  real cluster and verified for the first time against the actual
  `kubesentinel` ServiceAccount identity, not the admin kubeconfig every
  other live check in this milestone (and most of Milestones 13-17) used --
  the exact distinction TODO.md limitation #14 already flagged as worth
  not skipping. `kubectl auth can-i ... --as=system:serviceaccount:...`
  confirmed `list statefulsets/daemonsets/jobs` went from `no` to `yes`
  after applying the Role; `metrics.k8s.io` pods needed a real minted
  token and a direct HTTP call instead, because `kubectl auth can-i` is
  unreliable for aggregated APIs (it answered `no` for a resource that a
  real authenticated request to the same identity returned a real `200`
  with 5 pod metrics for) -- a reminder that `can-i`'s own answer isn't
  always authoritative for an aggregated API group, confirmed by checking
  the real HTTP response rather than trusting the tool.

**Two real bugs found while writing API error-code tests** (exactly the
kind of gap RESOURCE_OPTIMIZATION_DESIGN.md Section 12's "API validation/
error-code coverage" testing goal exists to catch) -- both direct
divergences from Section 8's own error-mapping table, written in
Milestone 12 but never actually implemented when the routes were built in
Milestone 16:
1. `GET /optimization/recommendations?category=...`/`?status=...` took
   plain `str | None` query params, so an invalid value (e.g.
   `?category=NOT_REAL`) silently matched nothing and returned `200 []`
   instead of the promised `422`. Fixed by typing both as their real Enums
   (`RecommendationCategory`/`RecommendationStatus`) in the route
   signature -- FastAPI/Pydantic now rejects an invalid value before the
   service layer ever sees it.
2. `POST /optimization/analyze` had no handling at all for the cluster
   itself failing mid-call -- confirmed earlier in this same session when a
   real, transient connectivity failure (the contention documented below)
   made a live `/analyze` call raise, and it surfaced as a bare,
   unhandled 500. Fixed by wrapping the call in `try`/`except Exception`
   and mapping to `503`, matching `_require_remediation_service()`'s
   existing "no cluster connection" convention and the promise Section 8
   already made. A merely-missing *metrics provider* is deliberately left
   alone by this `except` -- that's still an honest `200` with
   `metrics_available=False`, a different and already-correct code path.

Both fixes have their own new unit tests (`test_list_recommendations_invalid_category_is_422`,
`test_list_recommendations_invalid_status_is_422`,
`test_analyze_cluster_unreachable_mid_call_is_503`).

**Live-verified against the real cluster**, beyond what's already
described above: ran a real long-lived API server process through 4
`POST /optimization/analyze` calls spaced ~110s apart (minutes, not
mocked) against both new scenarios. By the 4th call (4 samples spanning
326s, crossing the default 3-sample/300s LIMITED_HISTORY threshold),
`oversized-resources-demo` correctly produced a real `CPU_OVERSIZED`
recommendation (proposed 10m, the configured floor, "well below the
current request of 1[000m]") and a real `MEMORY_OVERSIZED` recommendation
(proposed 16Mi, the configured floor, "well below the current request of
256Mi") -- genuine percentile/safety-margin/floor math running against
genuine metrics-server samples for the first time in this project, not a
mocked `RawUsageSample` list. `missing-requests-demo` correctly produced
`DATA_QUALITY` from the very first call (sample_count=1), never a proposed
number, and was correctly superseded (marked `EXPIRED`) by each subsequent
call's fresh row -- the supersession logic (Milestone 16) confirmed live
under real repeated analysis, not just the unit-mocked repeated-`analyze()`
tests. `pending-resources-demo`'s `SCHEDULING`/`ACTIVE_INCIDENT` flags
(Milestones 15/17) kept firing correctly across all 4 calls alongside the
new scenarios, confirming no regression in the existing checks.

**Tests**: 5 new unit tests (2 for the `422` fix, 1 for the `503` fix, plus
the `test_optimization_live_scenarios.py` fixtures/tests counted as
integration, not unit). `./scripts/test.sh`: ruff clean, mypy clean,
**410 unit tests passed** (407 Milestone 17 baseline + 3 new).

The full `--all` run was attempted three separate times and failed with
the same 10-13 tests each time -- the worst shared-VM contention seen yet
this session (`ai-platform-engineer-control-plane` sustained at 200-260%
CPU for an extended period, at one point taking down metrics-server itself
via probe timeouts, and even causing bare `kubectl get nodes` to fail with
a TLS handshake timeout/EOF; a `subprocess.TimeoutExpired` showed up in one
run, from `kubectl create token`'s own 10s timeout inside the RBAC test
fixture). Waiting for the cluster to answer `kubectl get nodes` twice in a
row before retrying did not prevent a third failure, confirming the
contention comes in bursts over the run's ~5-6 minute duration rather than
a level that clears and stays clear. Decisive confirmation that none of
this is a regression: every one of the exact tests that failed in the
third `--all` attempt (`test_observer_still_detects_after_pod_deleted_and_recreated_with_same_name`,
all 8 of `test_rbac_boundary.py`, both `test_optimization_inventory_live.py`
tests, this milestone's own `test_full_analyze_pass_never_issues_a_non_get_http_request`,
`test_restart_reuses_incident_identity_and_reconciles_metrics`) was re-run
together, immediately afterward, in one single isolated pytest invocation --
**23 passed, 0 failed, 0 errors** in 77s. This is the same documented
environmental issue as the "Environment note" after Milestone 11 and the
two prior milestones' writeups, now at its worst severity this session --
not a regression, and not something to chase further here; a future
session on a quieter machine (or with the unrelated cluster stopped first)
should get a clean single-pass `--all` run.

## Milestone 19: Documentation and end-to-end demo -- DONE

The resource optimization engine was, until this milestone, entirely
undocumented outside TODO.md and its own design doc (confirmed by grepping
every existing doc for "optimiz" before starting -- zero hits anywhere
else). Updated the *existing* docs rather than writing new ones, per this
project's own established rule:

- **README.md** -- status line, a new paragraph describing the engine
  (alongside the existing troubleshooting/remediation/AI paragraphs), a
  second pipeline diagram, new Quickstart commands, `optimization/` and the
  new RBAC/lab files added to Project layout, updated test counts (442
  total: 410 unit + 32 integration/e2e, not the stale 257/234/23), and
  RESOURCE_OPTIMIZATION_DESIGN.md added to the Documentation list.
- **ARCHITECTURE.md** -- a full second "Optimization pipeline" section with
  its own ASCII diagram (inventory -> metrics providers -> analyzer ->
  safety -> service -> API/CLI/persistence), 3 new "Why this shape" bullets
  (read-only by construction not convention; CPU-vs-memory statistic
  asymmetry; reviewable records with status computed at read time), and 3
  new "What's deliberately not built" bullets (apply/execute path, cluster
  overcommitment analysis, a Node-metrics ClusterRole).
- **DEMO_SCRIPT.md** -- a new step 8b walking the full optimization
  engine demo (apply the two new scenarios, optionally install
  metrics-server, run `optimize` across several minutes to cross the
  `LIMITED_HISTORY` threshold, show a real `CPU_OVERSIZED`/
  `MEMORY_OVERSIZED` recommendation, point out `ACTIVE_INCIDENT`, record a
  review decision) -- every command in it was actually run against the real
  cluster while writing it (the same "validate all documented commands"
  discipline that caught a real bug in Milestone 10), which caught two real
  doc-accuracy issues before they shipped: a misleading
  `KUBESENTINEL_API_TOKEN=` prefix on the CLI review command (the token only
  gates the *HTTP* route; the CLI never checks it, same as `approve`/
  `execute` already don't), and the recommendations table showing only an
  8-character truncated ID that `kubesentinel recommendation <id>` doesn't
  actually accept -- fixed by pointing the doc at the full ID from
  `--json` output instead, both caught by literally hitting "no
  recommendation <id>" live rather than assuming the commands would work.
- **DESIGN_DECISIONS.md** -- 6 new entries matching the existing format: no
  apply/execute endpoint; percentile-for-CPU/peak-for-memory; the 3-sample/
  5-minute threshold before any proposed number; supersession only ever
  touching a still-`NEW` row; `EXPIRED` computed at read time, never by a
  background job; and the `ACTIVE_INCIDENT` check covering both a
  workload's own UID and every current Pod's UID.
- **KUBERNETES_CONCEPTS.md** -- 2 new sections: owner-reference resolution
  (the two-hop Pod -> ReplicaSet -> Deployment walk) plus the
  `omitempty`-absence replica-count gotcha, and the Metrics API as an
  aggregated API with its own failure mode (down independently of the
  control plane) and its own `kubectl auth can-i` unreliability (see below).
- **RBAC_AND_THREAT_MODEL.md** -- the full `rbac/optimization-role.yaml`
  listing as a third, independently-auditable Role, a new Trust-boundary
  bullet for the optimization engine's read-only-by-construction guarantee,
  and an updated automated-check count (20, not 16) with a note on the
  `can-i`-vs-aggregated-API finding below.
- **FAILURE_LAB.md** -- two new scenario sections (`oversized-resources`,
  `missing-requests`) matching the existing format, explicitly flagged as
  not failure scenarios in the troubleshooting-engine sense (the Pods stay
  healthy throughout).
- **TESTING.md** -- updated top-line counts (442, not the Milestone-11-era
  257), a full new "Resource optimization engine unit test coverage"
  section (176 tests across 9 files, each count independently verified via
  `pytest --collect-only` rather than counted by hand), and updated
  Integration-tests bullets for the expanded `test_rbac_boundary.py` (20
  tests) and the new `test_optimization_inventory_live.py`/
  `test_optimization_live_scenarios.py` (5 tests).

**A real gap found and closed while updating RBAC_AND_THREAT_MODEL.md**:
that doc's own stated philosophy is "a one-off manual RBAC finding should
become a repeatable automated test, not stay a one-time check" -- but
Milestone 18's live RBAC verification of `rbac/optimization-role.yaml`
(StatefulSets/DaemonSets/Jobs/`metrics.k8s.io`, and the `can-i`-vs-real-call
discrepancy for the aggregated API) had only ever been done manually via
`kubectl`, never turned into a test. Fixed by extending
`tests/conftest.py::kubesentinel_sa_clients` to also construct
`BatchV1Api`/`CustomObjectsApi` (a 4-tuple now, not 2 -- the 6 existing
call sites in `test_rbac_boundary.py` were updated to match) and adding 4
new tests: StatefulSets/DaemonSets/Jobs listable, pod-metrics reachable via
a *real authenticated request* rather than `can-i` (treating a `503` --
metrics-server itself down, a real state this project has hit live under
contention -- as inconclusive, not a failure), and the expected `patch`/
cross-namespace denials. All 4 passed live against the real cluster and
the real `kubesentinel` ServiceAccount, not the admin kubeconfig.

**Live-verified against the real cluster**: every optimization CLI command
in the new DEMO_SCRIPT.md step (`optimize`, `recommendations`,
`recommendation`, `optimization-summary`, `review-recommendation`) was run
for real, catching the two doc-accuracy bugs above. The full, expanded
`test_rbac_boundary.py` (20 tests, including the 4 new optimizer-Role
tests) ran clean against the real cluster and the real ServiceAccount.

**Tests**: 4 new integration tests (the optimizer-Role RBAC checks above).
`./scripts/test.sh`: ruff clean, mypy clean, **410 unit tests passed**
(unchanged from Milestone 18 -- this milestone added no new unit-level
behavior, only docs and RBAC-boundary coverage). `./scripts/test.sh --all`:
collected **442 tests** (410 unit + 32 integration/e2e, confirmed via
`pytest --collect-only`, not guessed). A single uninterrupted `--all` run
was attempted twice more this milestone and both times hit the same
shared-VM-contention pattern documented after Milestones 11, 17, and 18 (9
failed/2 errored the second time, all in tests that take a real HTTP round
trip -- the run itself took 392s, long enough for a contention burst to
land mid-run even starting from a calm 49-90% CPU reading); re-running
every one of those exact tests together in isolation immediately
afterward gave **27 passed, 0 failed, 0 errors** in 64s. This is the same
environmental issue, not a regression, confirmed for a fourth time this
session using the identical "isolate and re-run" method each time.

**Known limitations** (from RESOURCE_OPTIMIZATION_DESIGN.md and this
milestone's own doc pass, consolidated): no apply/execute path for a
recommendation (every number requires a human, or a future, explicitly-
built policy-engine path, to act on it); no cluster-level overcommitment
analysis; no Node-metrics ClusterRole (nothing calls
`get_current_node_usage()` yet); multi-namespace optimization inherits the
troubleshooting engine's own single-namespace live-watch limitation only
insofar as `KUBESENTINEL_WATCH_NAMESPACES` configures what `analyze()`
defaults to scanning -- `analyze()` itself still correctly lists every
namespace it's asked to, since it never watches at all.

This closes the extension's own 8-milestone plan
(RESOURCE_OPTIMIZATION_DESIGN.md §13): typed models and quantity handling,
workload inventory with correct ownership resolution, deterministic
CPU/memory analysis with safety checks, API/CLI/persistence integration,
incident integration with new Prometheus metrics and RBAC, live kind-lab
verification, and now documentation -- all 8 steps done, each with its own
live-verification evidence recorded above (Milestones 12-19 in this file's
own numbering).

## Milestone 20: Platform spec Milestone 0 -- repository audit -- DONE

A new, broader platform specification arrived asking KubeSentinel to grow
into a multi-capability reliability/optimization platform: scheduling
advisor, network troubleshooting, capacity planning, security auditing, an
operator, and a chaos-lab evaluation harness, on top of the troubleshooting
engine and resource optimization engine already built. Per the spec's own
explicit instruction ("Start with Milestone 0 only"), this milestone was
audit-only -- no code changed.

Ran the baseline test suite (410 unit tests passed, ruff/mypy clean;
`--all` affected by this session's now-extensively-documented shared-VM
contention, cited against the already-verified 442-test baseline rather
than re-chased), inspected every existing package against the spec's own
capability list (sections 2-12), and produced three new docs:
`docs/IMPLEMENTATION_STATUS.md` (a capability-by-capability matrix, every
"Implemented" claim naming the file(s) that back it), `docs/MASTER_ARCHITECTURE.md`
(the one shared pattern every analysis engine should follow, stated once),
and `docs/PLATFORM_ROADMAP.md` (the spec's own Milestone 0-9 numbering,
adapted to this repo's actual structure) -- cross-referencing, not
duplicating, the existing ARCHITECTURE.md/TODO.md/RESOURCE_OPTIMIZATION_DESIGN.md.

**Real findings from the audit, not just a status restatement**:
- `UsageSummary.throttled` (resource optimization engine) is scaffolded but
  never populated -- always `None`. A real, previously-undocumented gap.
- The lab's kind cluster is single-node and uses kindnet, which doesn't
  enforce NetworkPolicy -- two environment constraints that will shape
  Capability C (scheduling) and Capability D (network) respectively, found
  and documented *before* either was built, not discovered mid-build.

See `docs/IMPLEMENTATION_STATUS.md` for the full, evidence-backed audit.

## Milestone 21: Scheduling & Placement Advisor -- DONE

Per `docs/PLATFORM_ROADMAP.md`'s own sequencing (Milestone 3 of the
platform spec; Milestone 1's "stabilize the core" had nothing genuinely
incomplete to fix, confirmed by the Milestone 20 audit). Design-first, per
this project's own established precedent: `docs/SCHEDULING_ANALYSIS.md`
before any code, covering exactly which constraints would be evaluated for
real vs. only detected-and-reported, before implementation began.

Added:

- `scheduling/node_inventory.py` -- builds Node allocatable capacity and
  *requested* resources (the scheduler's own accounting unit: the sum of
  every other bound Pod's own requests, not live usage) in one list-only
  pass, the same "one-shot snapshot, no watch loop" shape
  `optimization/inventory.py` already established.
- `scheduling/constraints.py` -- five pure, independently-tested constraint
  functions: `RESOURCE_FIT` (reproduces the scheduler's own
  allocatable-minus-requested arithmetic), `TAINT_TOLERATION` (real
  effect/operator/value matching, `PreferNoSchedule` correctly treated as
  soft and never blocking), `NODE_SELECTOR` (exact label equality),
  `REQUIRED_NODE_AFFINITY` (all 6 real `NodeSelectorRequirement` operators,
  terms OR'd, expressions within a term AND'd), `NAMESPACE_QUOTA` (real
  ResourceQuota arithmetic). Four more constraint types (preferred node
  affinity, pod affinity, pod anti-affinity, topology spread) are
  *detected* and named in every finding that has one, never silently
  ignored and never guessed at -- the exact "explain and simulate, don't
  reimplement the scheduler" boundary the spec asked for, made structural
  (`EVALUATED_CONSTRAINT_TYPES` vs. everything else) rather than just
  described in prose.
- `scheduling/analyzer.py` -- orchestrates every evaluated constraint
  against every Node for a real Pod *or* a hypothetical spec. A
  hypothetical spec (a raw JSON dict) is deserialized into a real, typed
  `V1PodSpec` via the Kubernetes client's own deserializer
  (`ApiClient().__deserialize`) before analysis, so every constraint
  function reads real and hypothetical Pods through exactly the same
  attribute-based code, no second path. A real, deliberate design decision
  made during implementation, not anticipated in the design doc: the
  namespace-quota check only ever runs for a hypothetical spec, never a
  real already-existing Pod -- a real Pod already passed admission, so
  quota can't explain why it's Pending; checking it again would be
  meaningless, and the code enforces that distinction (`check_quota: bool`,
  never inferred from "quotas is non-empty") rather than leaving it to the
  caller to get right.
- `scheduling/service.py` -- `SchedulingService`, the one module with side
  effects, mirroring `OptimizationService` exactly: `analyze_pod()` (a real
  Pod, resolves the owning workload via `optimization/ownership.py::resolve_pod_owner`,
  reused by import), `analyze_hypothetical()`, `get_finding()`/`list_findings()`.
  Wraps each analyze path in a try/except recording
  `kubesentinel_scheduling_analysis_total{result}` (schedulable/
  unschedulable/failed), the same pattern `OptimizationService.analyze()`
  already established.
- `models/scheduling.py`, `persistence/scheduling_repo.py`
  (`SQLiteSchedulingRepository`, a third sibling behind
  `persistence/interface.py`), `api/scheduling_routes.py` (`POST
  /scheduling/analyze`, `POST /scheduling/simulate`, `GET
  /scheduling/findings[/{id}]`), and 5 new CLI commands
  (`schedule-check`, `scheduling-findings`, `scheduling-finding`) -- all
  following the exact pattern `MASTER_ARCHITECTURE.md` describes, now
  proven against a third capability, not just a second.
- `rbac/scheduling-role.yaml` -- the smallest Role this project has added
  yet: `resourcequotas` (get/list) is the only genuinely new grant: Nodes,
  Pods/Deployments, and ReplicaSets were already granted to the same
  ServiceAccount by three *other* existing Roles, reused rather than
  re-granted.
- A new lab scenario, `scenario-invalid-node-selector` (an impossible
  `nodeSelector`), chosen specifically because no existing scenario
  exercises a non-resource scheduling constraint --
  `pending-resources-demo` already covered the resource-shortfall case.

**A real gap found and fixed within this same milestone, by this
project's own Milestone 20 audit standard**: `SchedulingFinding.related_incident_ids`
was declared in the design doc and the model from the start (mirroring
`ResourceRecommendation`'s own incident link), but `SchedulingService` had
no `incident_repo` parameter at all -- the field would have stayed `[]` on
every finding, the exact "scaffolded but never populated" shape Milestone
20's audit flagged for `UsageSummary.throttled` in the *other* engine.
Caught by live-checking the field against a real Pod with a genuinely open
incident rather than trusting the design doc's own description of what the
model *should* do. Fixed by adding `SchedulingService._open_incident_ids_for()`
-- the exact same dual-UID (Pod's own UID, owning workload's UID), deduped,
no-repo-wired-up-is-a-safe-no-op lookup `OptimizationService` already
established -- and wiring `incident_repo=state.repo`/a fresh
`SQLiteIncidentRepository` into the API and CLI construction paths, the
same way the optimization engine's own `_build_optimization_service()`
already does. Re-verified live immediately after: `schedule-check` against
the real `pending-resources-demo` Pod now correctly shows
`Related incident(s): ea2cc5a6-...`, matching the real open incident on its
owning Deployment. 4 new unit tests
(`TestIncidentLinking` in `test_scheduling_service.py`) cover the no-repo
no-op case, a match on the Pod's own UID, no match, and persistence.

**Live-verified against the real cluster**, not just unit-tested: both
`pending-resources-demo` (existing) and the new `invalid-node-selector-demo`
Pods analyzed with real Node numbers (4000m CPU / ~5.1Gi memory
allocatable, ~2150m/~950Mi already requested by other lab Pods at analysis
time) -- the advisor correctly isolated `RESOURCE_FIT` as the sole blocker
for the former and `NODE_SELECTOR` as the sole blocker for the latter
(`RESOURCE_FIT` passing), confirming it distinguishes *which* constraint
blocked placement, not just that scheduling failed. Owner-reference
resolution correctly named the owning Deployment for both. The HTTP
`/scheduling/simulate` endpoint confirmed live: a fitting hypothetical
spec returns `schedulable: true`; an impossible one (100 cores) correctly
returns `false`; a malformed spec (`"containers": "not-a-list"`) correctly
returns `422`, distinct from a `503` cluster-unreachable failure (`service.py::InvalidPodSpecError`
is a separate exception type specifically so the API layer can tell these
apart, mirroring the optimization engine's own 422-vs-503 distinction from
Milestone 18). The new RBAC Role verified against the real
`kubesentinel` ServiceAccount, not just admin: `resourcequotas` list went
from `no` to `yes` after applying it.

**Tests**: 92 new unit tests (`test_scheduling_constraints.py`: 32,
`test_scheduling_node_inventory.py`: 10, `test_scheduling_analyzer.py`: 19,
`test_scheduling_repo.py`: 7, `test_scheduling_service.py`: 15 -- 11 plus
the 4 `TestIncidentLinking` tests added for the gap above,
`test_scheduling_api.py`: 9) plus 4 new live integration tests
(`test_scheduling_live.py`) plus 3 new RBAC boundary tests. `make_node`
(the shared test factory) extended additively with `allocatable`/
`taints`/`labels` -- every pre-existing caller unaffected, confirmed by
re-running the full pre-existing node-related test set before adding
anything new. `./scripts/test.sh`: ruff clean, mypy clean (74 source
files), **502 unit tests passed** (410 + 92). `./scripts/test.sh --all`:
collected **541 tests** (502 unit + 39 integration/e2e). A single
uninterrupted `--all` run hit the same shared-VM-contention pattern
documented repeatedly throughout this project's history (13 failed/2
errored, all contention-class failures -- the exact same test names that
have failed this way before, none of them touching anything this milestone
changed); every one of those exact tests passed cleanly (28 passed, 2
skipped for unrelated preconditions, 0 failed) when re-run together in
isolation immediately afterward. This is the sixth confirmation of this
exact environmental pattern across this project's history, using the
identical "isolate and re-run" method each time.

**Documentation**: README.md, ARCHITECTURE.md (left untouched by design --
`docs/SCHEDULING_ANALYSIS.md` is Capability C's own design doc, the same
way RESOURCE_OPTIMIZATION_DESIGN.md is Capability B's, rather than growing
ARCHITECTURE.md into a fourth pipeline diagram it doesn't need),
IMPLEMENTATION_STATUS.md, PLATFORM_ROADMAP.md, TESTING.md,
RBAC_AND_THREAT_MODEL.md, FAILURE_LAB.md, and DEMO_SCRIPT.md (a new step
8c, every command in it actually run against the real cluster while
writing it, matching this project's own "validate all documented
commands" discipline).

## Milestone 22: Network Troubleshooting Engine -- DONE

Per `docs/PLATFORM_ROADMAP.md`'s own sequencing (Milestone 4 of the
platform spec). Design-first, per this project's own established
precedent: `docs/NETWORK_DIAGNOSTICS.md` before any code, covering the
exact data model, scope split (what's evaluated for real vs. deliberately
deferred), and API/CLI surface before implementation began.

Added:

- `network/service_inventory.py` -- `build_service_match_results()`:
  Service-selector-vs-Pod-label matching computed *independently* for
  every Pod in the namespace (exact label equality), never inferred from
  EndpointSlice counts the way the existing `ServiceSelectorMismatchRule`
  does. A second, separate check compares this independently-computed
  ready-Pod count against the real EndpointSlice controller's own
  ready-endpoint count (`endpointslice_consistent`) -- disagreement is
  itself a genuine finding (controller lag or bug), not noise. A one-shot
  raw-JSON EndpointSlice LIST (`_list_endpointslices_raw()`) reuses
  `observer/watchers.py::_ListWatchLoop`'s own technique
  (`_preload_content=False` + `json.loads`), because the generated
  client's typed `V1EndpointSlice` model still raises on a genuinely-empty
  `endpoints` field (Go's `omitempty` on a nil slice) -- confirmed live
  against this project's own kind cluster, the same bug already documented
  from building the original `EndpointSliceWatcher`. `_resolve_target_port()`
  implements Kubernetes' real, asymmetric `targetPort` semantics precisely:
  a **numeric** `targetPort` always routes to that port on the Pod's
  network namespace whether or not any container *declares* it
  (declaration is purely informational); a **named** `targetPort` has no
  such fallback and must resolve to a real, declared `containerPort.name`
  or that Service port has no usable endpoint on that Pod at all -- the two
  cases are reported with different confidence, never collapsed into one
  bool.
- `network/policy_inventory.py` -- `build_applicable_policies()`: real
  `podSelector` evaluation, explicitly implementing the empty-selector
  semantics that are the *opposite* of a Service's own empty selector (a
  NetworkPolicy's empty/absent `podSelector` matches **every** Pod in the
  namespace, not none) -- written as its own explicit branch, not left to
  an accidental fallthrough. Reuses `scheduling/constraints.py::requirement_matches`
  (renamed from the private `_requirement_matches` specifically for this)
  for `V1LabelSelectorRequirement` evaluation, structurally identical to
  the `V1NodeSelectorRequirement` matching already proven for the
  scheduling advisor. Summarizes each policy's ingress/egress rule
  presence (deny-all vs. permits-matching-traffic) without ever claiming
  traffic is actually blocked.
- `network/dns_health.py` -- `check_dns_health()`: reads the real
  `kube-system/coredns` Deployment's ready/desired replica counts (with
  the same `ready_replicas or 0` omitempty-zero normalization already
  documented for `optimization/inventory.py`) and the real
  `kube-system/kube-dns` Service's own EndpointSlices (same raw-JSON
  technique as above). A new, narrow, explicitly-justified exception to
  this project's "lab namespace only" RBAC boundary -- the first
  capability needing read access outside `kubesentinel-lab` for anything
  other than the existing cluster-scoped Node `ClusterRole`.
- `network/analyzer.py` -- `analyze_network()`, a pure function over
  already-fetched evidence (zero Kubernetes clients, fully deterministic,
  confirmed by an explicit `test_determinism` test) -- the same
  "pure function over pre-computed facts" shape `optimization/safety.py`
  and `scheduling/analyzer.py` already established. Classification
  priority, deliberately designed and exhaustively tested: `DNS_UNHEALTHY`
  > `CONFIGURATION_ERROR` (an unmatched port OR an EndpointSlice
  inconsistency) > `MISSING_OR_UNREADY_ENDPOINTS` > `POLICY_MAY_RESTRICT`
  (an applicable NetworkPolicy exists) > `HEALTHY`, with `INCONCLUSIVE` as
  its own separate branch for "no Service selects this Pod at all"
  (nothing to validate, not the same as "validated and fine"). Every
  finding's `reasoning` always appends the same caveat: this is
  evidence-based analysis of Kubernetes object state, never a confirmed
  end-to-end connectivity test -- API inspection cannot prove a packet
  actually arrives.
- `network/service.py` -- `NetworkService`, the one module with side
  effects, mirroring `SchedulingService`/`OptimizationService` exactly:
  `analyze_pod()` (resolves the owning workload via
  `optimization/ownership.py::resolve_pod_owner`, reused by import),
  `get_finding()`/`list_findings()`. Incident linking
  (`_open_incident_ids_for()`, the same dual-UID dedup pattern) was built
  in from the start this time, with its own unit tests from the first
  commit -- not found missing after the fact the way
  `SchedulingFinding.related_incident_ids` was in Milestone 21. Wraps
  `analyze_pod()` in a try/except recording
  `kubesentinel_network_analysis_total{result}`
  (healthy/configuration_error/missing_or_unready_endpoints/dns_unhealthy/
  policy_may_restrict/inconclusive/failed).
- `models/network.py`, `persistence/network_repo.py`
  (`SQLiteNetworkRepository`, a fourth sibling behind
  `persistence/interface.py`), `api/network_routes.py` (`POST
  /network/analyze`, `GET /network/findings[/{id}]` -- the
  `classification` query filter is a typed Enum from the start, applying
  Milestone 18's own 422-vs-silent-empty-list lesson rather than
  rediscovering it), and 3 new CLI commands (`network-check`,
  `network-findings`, `network-finding`) -- all following the exact
  pattern `MASTER_ARCHITECTURE.md` describes, now proven against a fourth
  capability.
- `observer/client.py::ClusterClients` -- `networking: NetworkingV1Api`
  added (new, additive); confirmed via grep that `build_clients()` is the
  only direct `ClusterClients(...)` construction site in the codebase, so
  no other call site needed updating.
- `rbac/network-role.yaml` (`services`/`networkpolicies` get/list, lab
  namespace) and `rbac/network-dns-role.yaml` +
  `rbac/network-dns-rolebinding.yaml` (the `kube-system` exception: `get`
  on exactly `coredns`/`kube-dns` by name via `resourceNames`, `list` on
  EndpointSlices). The DNS RoleBinding is bound in `kube-system` itself,
  not `kubesentinel-lab` -- a RoleBinding can only reference a Role in its
  *own* namespace, while its subject (the `kubesentinel` ServiceAccount)
  still lives in `kubesentinel-lab`, the first time this project's RBAC
  needed a binding outside the lab namespace for anything other than the
  existing cluster-scoped Node `ClusterRole`.
- A new lab scenario, `scenario-service-port-mismatch` (a Deployment whose
  Pod declares container port `http`, paired with a Service whose
  `targetPort: web` never resolves), chosen specifically because the
  existing `service-selector-mismatch` scenario's selector matches zero
  Pods -- this milestone needed a scenario where the selector genuinely
  matches a real, Ready Pod, isolating the port-mismatch path specifically.

**Deliberate scope decisions, made during this milestone's own design
pass, not oversights**: the spec's "bounded, explicitly enabled diagnostic
mechanism" active DNS/connectivity probe was scoped out entirely --
reasoned as this project's first mutation capability outside the fully
policy-gated `RemediationExecutor`, deserving its own dedicated
design/safety review rather than folding a new mutation surface into an
already-large static-analysis milestone. Ingress/Gateway support was also
excluded, confirmed live (`kubectl get ingressclass`, `kubectl get crd`)
rather than assumed: zero such resources or CRDs exist anywhere in this
project's cluster or manifest history, making it an honest "not
applicable," not a built-but-silent gap.

**A real correction caught live during this milestone's own
verification, by this project's own Milestone 20/21 audit standard**: the
design doc and the new scenario's own first-draft comment assumed an
unresolvable named `targetPort` would leave the EndpointSlice with zero
ready endpoints (mirroring `service-selector-mismatch`'s zero-endpoint
shape). Live-checking the real EndpointSlice
(`kubectl get endpointslices -o yaml`) showed this was wrong: the
controller still adds a `ready: true` address for the genuinely-matched,
genuinely-Ready Pod -- just with `ports: null`, since it still can't
resolve the port. `endpointslice_consistent` therefore correctly stays
`true` for this scenario; the engine still reaches `CONFIGURATION_ERROR`,
but via the independent port-match check alone, not via the
EndpointSlice-consistency check as first assumed. Corrected in the
scenario manifest's own comment, `FAILURE_LAB.md`, and
`IMPLEMENTATION_STATUS.md` rather than left documented-wrong -- caught
before shipping, not after, by live-checking rather than trusting the
design doc's own prediction.

**Live-verified against the real cluster**, not just unit-tested: the new
`port-mismatch-demo` Pod (real, Ready, genuinely selected by its Service)
correctly classified `CONFIGURATION_ERROR` from the named-`targetPort`
mismatch alone, via both `kubesentinel network-check` and a real HTTP
server (`POST /network/analyze`, `GET /network/findings[/{id}]`,
including the `classification` filter's `422` on an invalid value and its
namespace filter); its owning Deployment was correctly resolved. The
unrelated `remediation-test` Pod (selected by no Service at all) correctly
returned `INCONCLUSIVE`. DNS health correctly read the real `coredns`
Deployment (2/2 ready) and `kube-dns` Service (2 ready endpoints) live,
through the new `kube-system`-scoped RBAC grant -- confirmed via `kubectl
auth can-i get deployments.apps/coredns -n kube-system --as=...` using the
real `TYPE/NAME` resourceNames syntax (plain `can-i get deployments.apps
-n kube-system`, with no resource name, correctly reports `no` for this
exact grant, the same `metrics.k8s.io`-style false-negative lesson from
Milestone 18 applied here from the start rather than rediscovered the hard
way; `tests/conftest.py::kubectl_can_i()` gained an optional
`resource_name` parameter for this). The two new Roles verified against
the real `kubesentinel` ServiceAccount, not just admin: `services`/
`networkpolicies` list went from `no` to `yes` after applying
`network-role.yaml`, and the `kube-system` exception was confirmed scoped
to exactly `coredns`/`kube-dns` by name (a different kube-system
Deployment by name, and Pods/Secrets generally, all correctly denied).

**Tests**: 64 new unit tests (`test_network_service_inventory.py`: 13,
`test_network_policy_inventory.py`: 9, `test_network_dns_health.py`: 6,
`test_network_analyzer.py`: 12, `test_network_repo.py`: 7,
`test_network_service.py`: 9, `test_network_api.py`: 8) plus 5 new live
integration tests (`test_network_live.py`) plus 7 new RBAC boundary
tests. `./scripts/test.sh`: ruff clean, mypy clean (83 source files),
**566 unit tests passed** (502 + 64). `./scripts/test.sh --all`: collected
**617 tests** (566 unit + 51 integration/e2e). Two uninterrupted `--all`
runs each hit the same shared-VM-contention pattern documented repeatedly
throughout this project's history (21 failed/4 errored on the first run,
including tests entirely unrelated to this milestone's own changes --
`test_optimization_inventory_live.py`, `test_observer_live_detection.py`
-- all contention-class `kubectl` subprocess timeouts, not real
regressions); every one of the affected tests passed cleanly when
re-run together in isolation immediately afterward (40 passed, 2 skipped
for unrelated preconditions, 0 failed). This is at least the seventh
confirmation of this exact environmental pattern across this project's
history, using the identical "isolate and re-run" method each time.

**Documentation**: README.md, PLATFORM_ROADMAP.md, IMPLEMENTATION_STATUS.md,
MASTER_ARCHITECTURE.md, TESTING.md, RBAC_AND_THREAT_MODEL.md,
FAILURE_LAB.md, and DEMO_SCRIPT.md (a new step 8d, every command in it
actually run against the real cluster while writing it, matching this
project's own "validate all documented commands" discipline).
ARCHITECTURE.md left untouched by design, same reasoning as Milestone 21 --
`docs/NETWORK_DIAGNOSTICS.md` is Capability D's own design doc.

## Milestone 23: Capacity Planning & Autoscaling Advisor -- DONE

Per `docs/PLATFORM_ROADMAP.md`'s own sequencing (Milestone 5 of the
platform spec). Design-first, per this project's own established
precedent: `docs/CAPACITY_PLANNING.md` before any code -- which, unlike
every prior capability's design doc, concluded the right shape was *not*
a new top-level package at all, following the platform spec's own
explicit instruction not to build a second metrics collector.

Added:

- `optimization/capacity.py` -- a new submodule inside the existing
  `optimization/` package, not a new `capacity/` package. `build_hpa_inventory()`
  (one-shot HPA LIST via the new `AutoscalingV2Api`, same "no watch loop"
  shape every other inventory function in this project already uses),
  `attach_hpa_snapshots()` (pure, matches an HPA to a `WorkloadSnapshot`
  by scale-target kind+name -- an HPA has no UID reference to its
  target, only a name), `compute_trend()` (a plain two-half-mean
  comparison over an already-time-sorted sample list, explicitly never
  a forecast), `detect_bottleneck_pattern()` (a narrow single-threaded-
  ceiling pattern match computed from *this call's own fresh* per-Pod
  samples, deliberately not the cross-replica pooled bag
  `analyzer.py` uses for CPU/MEMORY sizing -- pooling across replicas
  would hide exactly the per-replica signal this check needs),
  `compute_scale_headroom()` (a coarse, explicitly-approximate greedy
  first-fit against real Node allocatable-minus-requested totals, reused
  by import from `scheduling/node_inventory.py::build_node_inventory` --
  the first time `optimization/` has imported from `scheduling/`, the
  opposite direction from Milestone 22's network-engine reuse of
  scheduling's own constraint-matching code), and `analyze_capacity()`
  (the one public entry point, mirroring `analyzer.py::analyze_workload()`'s
  "pure function over already-fetched facts" shape, returning at most
  one `ResourceRecommendation` per workload -- replica capacity is a
  workload-level concern, not per-container, using
  `workload.containers[0]` as the representative container for the row,
  the same precedent `_scheduling_recommendation` already established).
- `models/optimization.py` -- additive only, no new model file:
  `TrendDirection` enum, `HpaMetricStatus`/`HpaSnapshot` models, one new
  `RecommendationCategory.CAPACITY` enum value, and one new optional
  `WorkloadSnapshot.hpa` field.
- `observer/client.py::ClusterClients` -- `autoscaling: AutoscalingV2Api`
  added (new, additive).
- `config.py` -- seven new `capacity_*` settings, same conservative-
  defaults discipline as every existing `optimization_*` threshold
  (e.g. a leak pattern needs a window longer than the general history
  minimum before it fires, specifically to avoid reading normal
  warm-up/traffic noise as a leak).
- `optimization/service.py::OptimizationService.analyze()` -- extended
  (not replaced) to build the HPA inventory and Node inventory once per
  pass, call `analyze_capacity()` alongside the existing
  `analyze_workload()` call for every workload, and run its result
  through the exact same generic `safety.py::apply_safety_checks()`
  every other category already goes through. A Node-inventory read
  failure degrades only the one headroom check (logged, `node_snapshots=[]`)
  rather than aborting the entire `analyze()` pass -- this project's
  established "degrade honestly, never abort the whole pass for one
  optional sub-check" pattern, applied here for the first time to a
  cross-package dependency rather than a metrics provider.
- `rbac/capacity-role.yaml` + `rbac/capacity-rolebinding.yaml` -- the
  smallest Role added since `scheduling-role.yaml`, for the identical
  reason: every other permission this code needs (Nodes, Pods,
  Deployments, etc.) is already granted elsewhere to the same
  ServiceAccount; the only genuinely new grant is
  `horizontalpodautoscalers.autoscaling` (get/list).
- A new lab scenario, `scenario-hpa-capacity-pressure` (a real HPA pinned
  at `minReplicas=maxReplicas=1` with a deliberately tiny 1% CPU target,
  paired with a CPU-bound busy-loop container), chosen specifically to
  make the "at max and still over target" condition reproducible
  deterministically without needing sustained synthetic load generation
  beyond the busy-loop itself.

**Zero new persistence, API routes, or CLI commands were needed** --
confirmed by reading the persistence layer (`WorkloadSnapshot`/
`ResourceRecommendation` are JSON-blob-serialized as a whole; a new
optional field and a new enum value need no schema migration) and the
API layer (`category` is already a typed `RecommendationCategory` Enum
query param, so `?category=CAPACITY` works, 422-on-invalid included, the
moment the enum gains the value; `GET /optimization/workloads` already
returns full `WorkloadSnapshot` dicts, so the new `hpa` field appears
there automatically). The only line of CLI code touched was the
`--category` help string.

**Two real corrections, caught live during this milestone's own build,
by this project's own Milestone 20/21/22 audit standard**: the scenario's
first draft used an idle (`sleep infinity`) container, reasoned to
trivially exceed a 1% CPU target. Live-checking showed metrics-server
rounds real usage below roughly 1m CPU down to 0%, so an idle container
never exceeded even that absurdly low target -- fixed by giving the
container a real (if synthetic) CPU-bound workload instead. Second: once
fixed, editing the already-running Deployment's template in place (rather
than deleting and recreating it) deadlocked a rolling update -- the new,
large-request Pod couldn't schedule while the old one still held the
resources, and the old one wasn't replaced until the new one became
Ready. Both are documented in the scenario manifest's own comments for
future maintainers, not just fixed silently.

**Live-verified against the real cluster**, not just unit-tested: the
`hpa-capacity-pressure-demo` scenario correctly produced a `CAPACITY`
finding with `HPA_AT_MAX`, reading the real HPA's current utilization
(~54-58%) against its real 1% target and its real
`ScalingLimited`/`TooManyReplicas` condition, confirmed via
`kubesentinel optimize`/`kubesentinel recommendations --category
CAPACITY`/`kubesentinel recommendation <id>`, a real HTTP server's `POST
/optimization/analyze` and `GET /optimization/recommendations?category=CAPACITY`,
and `GET /optimization/workloads` exposing the live `HpaSnapshot` with no
route changes. `SCALING_WOULD_BE_UNSCHEDULABLE` also fired live, from
this lab's real Node allocation (3860m/4000m requested at analysis time,
confirmed via `kubectl describe node`) -- honestly documented as
environment-dependent (this specific sub-finding depends on the lab's
other scenarios' own current CPU usage, not deterministic in every
environment) rather than claimed as a guaranteed result. The two new RBAC
grants verified against the real `kubesentinel` ServiceAccount, not just
admin: `horizontalpodautoscalers.autoscaling` list/get went from `no` to
`yes` after applying `capacity-role.yaml`, with no `resourceNames`-style
false-negative the way the network engine's `kube-system` grant needed a
workaround for -- confirmed live that plain `can-i` answers correctly for
this ordinary, non-aggregated API group.

**An environmental incident during this milestone's own live
verification, worth recording because it was root-caused rather than
worked around blindly**: a full `--all` run produced 21+ contention-class
failures including a genuine `cluster-admin ClusterRole not found` error
for `kubernetes-admin` itself (not just the `kubesentinel` ServiceAccount)
-- initially indistinguishable from the usual `kubectl` subprocess-timeout
pattern already documented repeatedly in this project's history. Checked
`docker stats` directly rather than assuming: this machine was running
two *other*, entirely unrelated kind/Jenkins containers
(`ai-platform-engineer-control-plane` at up to 267% CPU / 2.6GB,
`ai-platform-engineer-jenkins`) alongside this project's own
`kubesentinel-control-plane`, leaving under 200MB of system RAM free and
causing the control plane's own components (`kube-apiserver`,
`kube-controller-manager`, `kube-scheduler`) to crash-loop from resource
starvation -- confirmed via `crictl ps -a` showing `kube-apiserver` at
restart attempt 31. This is the same "shared-VM contention" pattern
already named repeatedly in this project's history, now confirmed with
direct host-level evidence rather than inferred from symptoms alone. This
project's own `scenario-hpa-capacity-pressure` (a continuous CPU
busy-loop, by design) was a secondary, smaller contributor to the same
container's own CPU pressure -- scaled to 0 replicas just long enough to
let the apiserver recover and re-verify cleanly, then restored to its
documented 1 replica before this milestone's work was considered
complete. Every affected test passed cleanly (43 passed, 1 skipped, 0
failed) once re-run in isolation after the host's other load subsided.
This is at least the eighth confirmation of this exact environmental
pattern across this project's history, using the identical "isolate and
re-run" method each time, and the first time the root cause was
confirmed at the host level rather than only inferred from `kubectl`'s
own symptoms.

**Tests**: 45 new unit tests (`test_capacity.py`: 41, across
`TestHpaToSnapshot`/`TestBuildHpaInventory`/`TestAttachHpaSnapshots`/
`TestComputeTrend`/`TestDetectBottleneckPattern`/`TestComputeScaleHeadroom`/
`TestAnalyzeCapacity`; plus 4 new tests in `test_optimization_service.py`'s
`TestCapacityIntegration`, confirming the service-level wiring and that
a Node-inventory failure degrades gracefully) plus 3 new live integration
tests (`test_capacity_live.py`) plus 3 new RBAC boundary tests.
`./scripts/test.sh`: ruff clean, mypy clean (85 source files), **611
unit tests passed** (566 + 45). `./scripts/test.sh --all`: collected
**668 tests** (611 unit + 57 integration/e2e). See the environmental
incident above for the one full-suite run's contention-class failures
and their clean isolated re-run.

**Documentation**: README.md, PLATFORM_ROADMAP.md, IMPLEMENTATION_STATUS.md,
MASTER_ARCHITECTURE.md (including a note on this milestone being the
third documented exception to the "its own package" rule, following the
platform spec's own instruction rather than this project's own
judgment), TESTING.md, RBAC_AND_THREAT_MODEL.md, FAILURE_LAB.md, and
DEMO_SCRIPT.md (a new step 8e, every command in it actually run against
the real cluster while writing it, matching this project's own "validate
all documented commands" discipline). ARCHITECTURE.md left untouched by
design, same reasoning as Milestones 21/22 -- `docs/CAPACITY_PLANNING.md`
is Capability E's own design doc.

## Milestone 24: Security & Configuration Auditor -- DONE

Per `docs/PLATFORM_ROADMAP.md`'s own sequencing (Milestone 6 of the
platform spec -- the full A-F capability list from the original platform
specification is now complete). Design-first, per this project's own
established precedent: `docs/SECURITY_AUDITING.md` before any code,
including a genuine build-vs-integrate decision (a native rule catalog,
not kube-linter/Kubescape -- see that doc's §2) made explicitly during
the design pass, not pre-decided or discovered mid-build.

Added:

- `security/inventory.py` -- `build_security_inventory()`: a one-shot
  read of Pods grouped by owning workload (reusing
  `optimization/ownership.py::resolve_pod_owner`, now reused by a fourth
  capability), ClusterRoles/ClusterRoleBindings/Roles/RoleBindings,
  NetworkPolicies, and Deployments/StatefulSets/PodDisruptionBudgets. A
  sub-read that fails (e.g. a 403) is recorded in `SecurityInventory.warnings`
  and that one rule family evaluates against an empty list, rather than
  aborting the whole audit.
- `security/rules/container.py` -- 8 rules: `PRIVILEGED_CONTAINER`,
  `ALLOW_PRIVILEGE_ESCALATION` (explicit `true` vs. merely unset reported
  at different severities -- an absent field is itself evidence, the same
  discipline `analyzer.py::_data_quality_recommendation` already
  established for missing resource requests), `RUN_AS_ROOT` (effective
  value considering the real container-overrides-Pod-level inheritance
  rule), `MISSING_READ_ONLY_ROOT_FS`, `HOST_NAMESPACE_SHARING`,
  `HOST_PATH_VOLUME`, `MUTABLE_IMAGE_TAG` (reusing the exact `:latest`
  gotcha already documented in TROUBLESHOOTING.md for an unrelated
  reason, now also a security finding), `SECRET_AS_ENV_VAR`.
- `security/rules/rbac.py` -- 2 rules: `CLUSTER_ADMIN_BINDING` (CRITICAL
  cluster-wide / HIGH namespace-scoped) and `WILDCARD_RBAC_RULE` (HIGH),
  both excluding Kubernetes' own built-in, unfixable Roles/bindings --
  see the real correction below for why the exclusion needed a second
  pass.
- `security/rules/network_policy.py` -- 2 rules:
  `NAMESPACE_WITHOUT_NETWORK_POLICY` and `UNRESTRICTED_INGRESS_POLICY`.
  The latter reuses a newly-promoted-public
  `network/policy_inventory.py::has_unrestricted_ingress_rule()`
  (alongside `summarize_policy()`, `_summarize_policy` likewise promoted)
  rather than re-reading and re-classifying NetworkPolicy objects a
  second time, per the roadmap's own explicit instruction.
- `security/rules/availability.py` -- 1 rule:
  `MISSING_PDB_FOR_MULTI_REPLICA_WORKLOAD`, the roadmap's own named
  optional item, included since `PolicyV1Api` was simple to add. Its own
  `_pdb_selector_matches()` deliberately implements the *standard*
  Kubernetes LabelSelector semantics (null selector matches nothing,
  empty-but-present matches everything) -- the *opposite* null-handling
  from `network/policy_inventory.py`'s own podSelector matching, which
  defensively treats null as "matches all" for a documented,
  NetworkPolicy-specific reason. Reusing that function directly would
  have been a real, silent correctness bug for this different domain;
  written as its own small helper instead, reusing only
  `scheduling/constraints.py::requirement_matches` for the
  `matchExpressions` half (safely generic across both domains).
- `security/engine.py` -- `SecurityAuditEngine`, mirrors
  `diagnostics/engine.py::DiagnosticEngine` exactly. Two rules
  (`WildcardRbacRuleRule`, `NamespaceWithoutNetworkPolicyRule`,
  `ClusterAdminBindingRule`, `MissingPdbForMultiReplicaWorkloadRule`)
  need `Settings` for their own exclusion lists/thresholds, so
  `SecurityAuditEngine` builds its own rules list per instance rather
  than a static module-level singleton list the way
  `diagnostics/engine.py::DEFAULT_RULES` does (none of *its* rules need
  configuration) -- the same settings-threading shape
  `OptimizationService` already uses for its own pure functions.
- `security/service.py` -- `SecurityService`, the one module with side
  effects. Deliberately **no incident linking** -- unlike every other
  engine in this project, a security/config finding (an RBAC grant, a
  missing NetworkPolicy) has no single owning Pod-level incident the way
  a resource problem does.
- `models/security.py`, `persistence/security_repo.py`
  (`SQLiteSecurityRepository`, a fourth sibling behind
  `persistence/interface.py`), `api/security_routes.py` (`POST
  /security/audit`, `GET /security/findings[/{id}]`), and 3 new CLI
  commands (`security-audit`, `security-findings`, `security-finding`).
- `observer/client.py::ClusterClients` -- `rbac_authorization:
  RbacAuthorizationV1Api` and `policy: PolicyV1Api` added (new,
  additive); the client-surface table in MASTER_ARCHITECTURE.md is now
  fully populated for every capability A-F.
- `rbac/security-clusterrole.yaml` + `rbac/security-clusterrolebinding.yaml`
  (ClusterRole/ClusterRoleBinding read access -- the **third** documented
  cluster-scope exception in this project, after the Node ClusterRole and
  the network engine's kube-system-bound RoleBinding) and
  `rbac/security-role.yaml` + `rbac/security-rolebinding.yaml`
  (Roles/RoleBindings/PodDisruptionBudgets, lab namespace only).
- A new lab scenario, `scenario-security-findings` (a privileged
  container, a throwaway ServiceAccount bound to `cluster-admin`, and an
  unrestricted-ingress NetworkPolicy), chosen to exercise all three rule
  families with minimal, individually-verifiable manifests -- the
  existing `remediation-test`/lab-namespace state was reused as-is for
  the PDB-coverage and namespace-without-policy findings rather than
  building redundant new fixtures for those two.

**Two real findings caught live during this milestone's own
verification, by this project's own Milestone 20-23 audit standard, not
caught by unit tests alone**:

1. The RBAC built-in-subject exclusion first assumed only the well-known
   `system:masters` Group needed excluding. Live-checking this project's
   own lab cluster's real `ClusterRoleBindings` (`kubectl get
   clusterrolebindings`) showed a *second* genuine built-in binding --
   `kubeadm:cluster-admins`, the Group kubeadm itself uses for
   `admin.conf` -- being mis-flagged as a real finding. Fixed by
   excluding Group subjects by *prefix* (`system:`, `kubeadm:`,
   configurable via the new `security_builtin_subject_group_prefixes`
   setting) rather than one hardcoded exact name; a new unit test
   (`test_does_not_fire_for_builtin_kubeadm_cluster_admins_binding`) and
   a new live test (`test_builtin_rbac_bootstrap_bindings_are_never_flagged`)
   both pin this against a real value, not a mock.
2. `SecurityService.audit()` re-evaluates the entire cluster on every
   call, but the first persistence design -- an upsert-by-`finding_id`,
   copied directly from `NetworkFinding`/`SchedulingFinding`'s own
   per-*Pod* shape -- silently accumulated every past run's findings
   forever. Caught live, not in unit tests: applying the fix for gap #1
   above and re-running `kubesentinel security-audit` still showed the
   *stale*, now-incorrect `kubeadm:cluster-admins` finding from the
   previous run sitting in `kubesentinel security-findings` alongside the
   corrected result -- three `CLUSTER_ADMIN_BINDING` findings where only
   two should exist. Root cause: a full-cluster re-scan every call is a
   fundamentally different persistence shape from "one finding per Pod,"
   and the design had silently borrowed the wrong precedent. Fixed by
   replacing the per-finding upsert (`save_finding`) with a whole-run
   replace (`save_findings`, delete-then-insert), the same semantics
   `optimization_repo.py::save_workload_snapshots` already uses for its
   own full-cluster-rescan shape. New regression tests at both layers:
   `test_save_findings_replaces_the_entire_previous_set` (repo) and
   `test_second_audit_does_not_accumulate_stale_findings` (service and,
   live, `test_security_live.py`).

**A third, smaller gap found and fixed in the same pass, in shared lab
infrastructure rather than this milestone's own code**: building this
milestone's lab scenario (a NetworkPolicy, a ServiceAccount, a
ClusterRoleBinding) surfaced that `lab/scripts/cleanup.sh` only ever
deleted `deployment,pod,pvc,service` -- never `networkpolicy`/
`serviceaccount` (needed by this milestone) nor
`horizontalpodautoscaler` (needed since Milestone 23, three milestones
ago, and silently never cleaned up by any `cleanup.sh` run since).
ClusterRoleBinding/ClusterRole have no namespace at all, so a `-n
kubesentinel-lab`-scoped delete could never reach them regardless of the
resource list -- fixed with a second, explicit cluster-scoped delete
call. Confirmed live via `kubectl ... --dry-run=client` against the real
cluster showing every scenario-labeled resource across every milestone
(including the Milestone 23 HPA) now correctly targeted, not assumed from
reading the script alone.

**Live-verified against the real cluster**: the `scenario-security-findings`
scenario correctly produced `PRIVILEGED_CONTAINER`, `CLUSTER_ADMIN_BINDING`,
and `UNRESTRICTED_INGRESS_POLICY` findings with their real evidence named
explicitly, while the cluster's own two real built-in RBAC bootstrap
bindings were correctly never flagged. The pre-existing `remediation-test`
Deployment (2 replicas, no PDB) and the lab namespace's prior total
absence of any NetworkPolicy both triggered their own coverage findings
from existing state, with no new manifest needed for either. Confirmed
via `kubesentinel security-audit`/`security-findings --severity
CRITICAL`/`security-finding <id>`, a real HTTP server's `POST
/security/audit` and `GET /security/findings?severity=CRITICAL` (422 on
an invalid severity confirmed too), and that a second `audit()` call
leaves the repository showing only the current run's findings. The two
new RBAC grants verified against the real `kubesentinel` ServiceAccount,
not just admin: cluster-scoped `ClusterRole` checks (no namespace) and
namespaced `Role` checks both confirmed via `kubectl auth can-i
--as=...`, with the `horizontalpodautoscalers.autoscaling`-style
non-aggregated-API reliability already established in Milestone 23
extended here with zero new workarounds needed for
`rbac.authorization.k8s.io`/`policy` either.

**Tests**: 104 new unit tests across 8 files (`test_security_container_rules.py`:
31, `test_security_rbac_rules.py`: 14, `test_security_network_policy_rules.py`:
7, `test_security_availability_rule.py`: 11, `test_security_engine.py`: 7,
`test_security_inventory.py`: 6, `test_security_repo.py`: 11,
`test_security_service.py`: 9, `test_security_api.py`: 8 -- totals 104)
plus 6 new live integration tests (`test_security_live.py`) plus 5 new
RBAC boundary tests. `./scripts/test.sh`: ruff clean, mypy clean (97
source files), **715 unit tests passed** (611 + 104). `./scripts/test.sh
--all`: collected **783 tests** (715 unit + 68 integration/e2e). A full
`--all` run hit the same shared-VM-contention pattern documented
repeatedly throughout this project's history (29 failed/4 errored, every
one of them a pre-existing contention-class test unrelated to this
milestone's own new tests -- zero of the 110 new security-specific tests
failed); every affected test passed cleanly (59 passed) when re-run
together in isolation immediately afterward. This is at least the ninth
confirmation of this exact environmental pattern across this project's
history, using the identical "isolate and re-run" method each time.

**Documentation**: README.md, PLATFORM_ROADMAP.md, IMPLEMENTATION_STATUS.md,
MASTER_ARCHITECTURE.md (the client-surface table and the "proven N times"
count are both now complete across every platform-spec capability),
TESTING.md, RBAC_AND_THREAT_MODEL.md, FAILURE_LAB.md, and DEMO_SCRIPT.md.
ARCHITECTURE.md left untouched by design, same reasoning as every prior
milestone here -- `docs/SECURITY_AUDITING.md` is Capability F's own
design doc.

## Known limitations (found during Milestone 1's own live verification, not theoretical)

1. **Pod-UID dedup doesn't survive a Deployment rollout.** A Deployment template
   change (e.g. a real fix) creates new Pods with new UIDs. The old incident has no
   further events to tell it the old Pod recovered -- it stays open until an operator
   or a future reconciliation mechanism closes it. Pod-level incidents work correctly
   for in-place recovery (same UID, e.g. a transient CrashLoopBackOff clearing up);
   Deployment-level incident correlation is planned alongside Milestone 2's
   ROLLOUT_FAILED / UNSATISFIED_REPLICAS rules.
2. **A terminating Pod is excluded from observation entirely** (fixed in this
   milestone after finding it live: a Pod with `deletionTimestamp` set can transiently
   report container statuses with no "bad" waiting/termination reason while tearing
   down, which the health classifier would otherwise read as recovery). The fix skips
   any Pod with `deletionTimestamp` set rather than trying to interpret its transient
   state.
3. **PodWatcher's live watch stream only follows the first configured namespace**,
   not an arbitrary list of several (its periodic resync LIST does cover all of
   them, so other namespaces are picked up at most once per resync interval, not on
   every change). Fine for the single-namespace lab; needs a proper multi-watch-stream
   merge before it'd be safe for multiple arbitrary namespaces.
4. **`incidents_active` Prometheus gauge can drift** if an incident's diagnosed
   category changes between open and resolve (decrement uses the category *at resolve
   time*, which may differ from the one used at increment time). Not hit in the
   verified scenarios (category was stable throughout each), but a real gap.
5. **OOMKilled detection needed a fallback heuristic in this project's own dev
   environment**: this kind-on-Colima/macOS setup's kubelet reports OOM kills with the
   generic reason `Error` and `exit_code=137` rather than `OOMKilled`, confirmed
   against the node's own cgroup v2 `memory.events` `oom_kill` counter. The rule now
   treats a direct `OOMKilled` reason as HIGH-confidence observed fact and an inferred
   exit-code-137-with-repeated-restarts pattern as a separate, explicitly-labeled
   MEDIUM-confidence hypothesis. See DIAGNOSTIC_RULES.md.
6. **Diagnosis history dedup is coarse**: a materially-unchanged top hypothesis
   (same reason code) is not re-appended to `diagnosis_history`, which keeps the log
   readable but discards confidence/explanation refinements between observations of
   the same reason code.

## Known limitations (found during Milestone 2's own live verification)

7. **A Kubernetes Event outlives the resource it describes.** Event garbage
   collection runs on its own, longer TTL, independent of the described object's
   deletion. Found live: a `ProvisioningFailed` Event for an already-deleted PVC was
   still present on process restart and opened a phantom `PVC_BINDING_FAILURE`
   incident for a PVC that no longer existed. Fixed in
   `observer/service.py::ObserverService._still_exists` -- a live GET confirms the
   object is still there before an Event is allowed to open or enrich an incident.
   Only implemented for the two kinds Events are currently correlated against (Pod,
   PersistentVolumeClaim); a third kind added later needs its own branch there.
8. **`Deployment.status.conditions[Available]=False` does not mean "still rolling
   out."** Confirmed live: a Deployment whose Pods start crashing *after* a
   successful initial rollout shows `Progressing=True/NewReplicaSetAvailable` (the
   rollout itself genuinely succeeded) alongside `Available=False/
   MinimumReplicasUnavailable` -- the second condition is the one that actually
   reflects the ongoing problem, and `has_failure_signal` had to be extended to open
   on it directly (not just on `ProgressDeadlineExceeded`). Known tradeoff: a
   brand-new Deployment is also briefly `Available=False` while its first replicas
   start, so this can open a short-lived incident that resolves within the
   2-observation debounce once minimum availability is reached.
9. **Upstream bug in the generated `kubernetes` Python client (confirmed against
   installed version 30.1.0, not yet checked against the latest 37.x):** several
   `discovery.k8s.io/v1` models -- `V1EndpointSlice.endpoints` among them -- mark a
   list field as required and raise `ValueError` if the API server sends JSON
   `null` for it. The API server legitimately does this for a genuinely empty
   list (Go's `omitempty` on a nil slice serializes to `null`, not `[]`), which is
   exactly what a zero-endpoint EndpointSlice looks like -- confirmed live by
   fetching the raw JSON directly (`_preload_content=False`) and seeing
   `"endpoints": null` where the typed client crashed. This broke the *entire*
   list/watch call, not just one malformed object, and would have made
   `ServiceSelectorMismatchRule` non-functional for its primary case (zero
   endpoints) had it shipped unfixed. Worked around in
   `observer/watchers.py::_ListWatchLoop` (`raw_object_watch=True`): LIST fetches
   raw JSON directly; WATCH uses `kubernetes.watch.Watch(return_type="object")`,
   which the client's own `unmarshal_event`/`__deserialize` treats as a pass-through
   to `object` rather than attempting strict model validation. `EndpointSliceWatcher`
   and `endpointslice_evidence()` work on plain dicts as a result, not typed
   `V1EndpointSlice` instances -- the one place in this codebase where that's true.
   See TROUBLESHOOTING.md.

## Known limitations (found during Milestone 3's own live verification)

10. **The "Events outlive the resource" fix (#7) checked existence by name, not by
    the specific object.** Found live immediately after pulling real incident data
    for the policy-engine demo: a deleted-and-recreated `pvc-binding-demo` PVC (same
    name, new UID) produced *two* separate incidents -- correctly, per this
    project's UID-based dedup design -- but a stale `ProvisioningFailed` Event about
    the *first* (now-deleted) PVC was wrongly "revalidated" by
    `ObserverService._still_exists`, because a *different* PVC with the same name
    now existed under that name. Fixed by comparing the live object's UID against
    the UID the Event's `involved_object` actually names, not just checking that
    some object with that name exists. This is the exact name-reuse scenario
    UID-based dedup exists to handle correctly everywhere else in this codebase --
    the one spot that had regressed to name-based matching is now fixed too.

## Known limitations (found during Milestone 4's own live verification)

11. **Almost shipped a merge-patch body built from `.to_dict()`.** Caught before
    ever running it against the cluster, by reading `api_client.py`'s
    `sanitize_for_serialization` directly: a plain dict's values are sent through
    verbatim (no attribute-name conversion), so `V1PodTemplateSpec.to_dict()`
    nested inside a dict would have sent snake_case keys (`restart_policy`, not
    `restartPolicy`) *and* explicit `null` for every unset field -- which a merge
    patch would interpret as "clear this field," a far more destructive outcome
    than just a silently-ignored patch. Fixed by passing the typed model object
    directly (`{"spec": {"template": target_rs.spec.template}}`, no `.to_dict()`);
    the serializer only does the correct attribute-name conversion for nested
    typed objects, not for dicts.
12. **A `REJECTED` action still counted against cooldowns and per-incident/per-
    window budgets.** Found live running the real remediation demo: a scale-down
    cleanup step got `DENIED` by the per-incident budget, because two *successful*
    actions plus one *harmless, unrelated cooldown rejection* had already pushed
    the count to the budget ceiling -- even though the rejected action never
    touched the cluster. Left unfixed, a handful of transient rejections could
    permanently lock a real incident out of remediation. Fixed in
    `PolicyEngine._check_cooldown` / `_check_per_incident_budget` /
    `_check_per_window_budget`: all three now exclude actions with
    `status == ActionStatus.REJECTED` (but still count `EXECUTION_FAILED`, which
    did genuinely reach the cluster and is exactly what these limits exist to bound).
13. **Rollback used a merge PATCH and silently failed to remove a field the newer
    revision had added.** Found live: rolling back from a template with a restart
    annotation to an older template *without* one reported `EXECUTED` with a
    plausible-looking result, but the live Deployment's `spec.template` annotations
    were completely unchanged afterward. Root cause: the target (older) template's
    annotations field is `None`, which the client's serializer drops from the JSON
    entirely -- and under merge-patch semantics, an absent key means "no change,"
    not "clear it." Fixed by switching rollback to `replace_namespaced_deployment`
    (a full PUT of the whole Deployment object with `spec.template` mutated to the
    target revision's), which actually replaces the field rather than merging into
    it. Re-verified live after the fix: the restart annotation was confirmed gone
    and the original ReplicaSet correctly scaled back up.
14. **`rbac/remediation-role.yaml` granted `patch` but not `update` on
    `deployments`.** All of the live verification above ran under an admin
    kubeconfig, not the `kubesentinel-remediator` ServiceAccount, so it never
    actually exercised this Role's real boundary -- rollback's
    `replace_namespaced_deployment` call issues an HTTP PUT, which Kubernetes RBAC
    maps to the `update` verb, not `patch`. Confirmed live with `kubectl auth
    can-i update deployments --as=system:serviceaccount:kubesentinel-lab:
    kubesentinel -n kubesentinel-lab`: answered `no` before the fix, `yes` after
    adding `update` to the Role and re-applying it. A reminder that "it worked
    when I ran it" under a privileged kubeconfig is not the same claim as "the
    ServiceAccount this actually runs as in-cluster can do this."

## Known limitations (found during Milestone 5's own live verification)

15. **The first version of the verifier would have reported `RECOVERED` within
    0.1s of triggering a real restart -- while `kubectl get pods` still showed the
    new Pod `ContainerCreating` and both old Pods still `Running`.** Root cause:
    `is_confirmed_healthy()` (reused from Milestones 1-2) only checks
    `readyReplicas >= desired` and similar aggregates -- which during a rolling
    update can be satisfied entirely by the *old* ReplicaSet's still-serving Pods,
    well before the new one has done anything. That check is correct for the
    passive incident-manager use case it was built for ("is this Deployment
    healthy right now, regardless of why"), but wrong for "did *this specific
    restart/rollback* actually complete," which is what post-action verification
    needs to answer. Fixed by adding a separate, stricter `_rollout_complete()`
    check -- also requiring `updatedReplicas >= desired` -- used only for
    `RESTART_DEPLOYMENT`/`ROLLBACK_DEPLOYMENT` (both start a new rollout);
    `SCALE_DEPLOYMENT` (no new template, no new rollout) keeps using the simpler
    shared check. Re-verified live: the fixed version took 11.5s and the pod list
    confirmed the new ReplicaSet had genuinely taken over before reporting
    `RECOVERED`.

## Known limitations (found during Milestone 6's own live verification)

16. **Upstream version-compatibility bug between `typer==0.12.5` and `click`:
    every CLI command crashed.** `typer==0.12.5` declares only `click>=8.0.0`
    (no upper bound), so pip resolved the newest click (8.5.0) -- which changed
    `click.Argument.make_metavar()`'s signature (added a required `ctx`
    parameter). `typer`'s own `TyperArgument.make_metavar()` override wasn't
    updated to match, so the moment Click needed to render *any* usage or error
    text, every CLI invocation crashed with `TyperArgument.make_metavar() takes
    1 positional argument but 2 were given` -- including, confusingly, while
    trying to report an unrelated argument-parsing issue, which made the first
    few attempts look like a bug in my own command definitions rather than a
    dependency conflict. Confirmed by reading both packages' actual installed
    versions (`pip show typer click`) rather than guessing. Fixed by pinning
    `click==8.1.8` explicitly in `pyproject.toml` rather than leaving it to
    float -- exactly the "pin compatible dependency versions" the spec itself
    asked for, which this gap shows is worth taking literally even for
    dependencies-of-dependencies. See TROUBLESHOOTING.md.
17. **A routine `SCALE_DEPLOYMENT` 2->3 on a perfectly healthy Deployment was
    verified as `FAILED`.** Found live running the real CLI demo against
    `remediation-test`: one poll caught Kubernetes reporting
    `Available=False (MinimumReplicasUnavailable)` -- which is entirely normal
    for a few seconds while a newly-requested replica hasn't started yet, not
    evidence that anything is actually wrong. The Milestone 5 verifier reused
    `has_failure_signal` (correct for the *passive* incident manager, where
    Available=False observed with no context is a reasonable thing to open an
    incident about) for its FAILED path too -- but the *active* verifier is, by
    definition, watching a transition it just triggered, where a brief dip in
    availability is often exactly what recovering looks like. Fixed with a new,
    stricter `_deployment_definitively_stuck()` check used only for the
    verifier's FAILED path: it trusts `ProgressDeadlineExceeded` (set only after
    the controller has waited out its own deadline and given up -- a durable,
    legitimate "won't resolve on its own" signal) but not a bare
    `Available=False`. Re-verified live: the same scale action (this time
    scaling back down, 3->2) correctly reported `VERIFIED_RECOVERED`, confirmed
    against the real cluster afterward.
18. **`kubesentinel_incidents_active` read 0 (or undercounted) for incidents
    that predated the current process.** Found live while building the
    Milestone 7 Grafana dashboard and checking its panels against real
    `/metrics` output: a long-running demo server had 9 genuinely open
    incidents per `/incidents`, but `kubesentinel_incidents_active` showed
    nothing for most of their categories. Root cause: the gauge is only ever
    `.inc()`/`.dec()`'d by `IncidentManager._open_new`/`_resolve`, both of
    which run only on a *freshly detected* incident -- an incident already
    open in SQLite from a previous process restart is routed straight into
    `_update_existing` instead, which never touches the gauge, so it silently
    stayed at its process-local default. Fixed with
    `IncidentManager.reconcile_metrics()`, called once at API startup right
    after construction: it explicitly zeroes every known `ReasonCode` category
    (so a category is never silently absent from `/metrics`, which looks
    identical to "never happened" to a Grafana panel or an absence-based
    alert) and then sets each category with currently-open incidents to its
    real count from the repository. Re-verified live: restarted the real demo
    server against the existing `kubesentinel.db` (9 open incidents across 6
    categories) and confirmed `/metrics` showed the correct per-category
    counts (`CRASH_LOOP_BACKOFF=2`, `IMAGE_PULL_BACKOFF=2`,
    `UNSATISFIED_REPLICAS=2`, `ROLLOUT_FAILED=1`,
    `SERVICE_SELECTOR_MISMATCH=1`, `PVC_BINDING_FAILURE=1`, matching
    `/incidents` exactly) immediately on startup, before the observer's own
    resync had produced a single new event.
19. **A Pod with no `readinessProbe` at all can satisfy a Deployment rollout's
    "available" check for a moment before it crashes, which silently prevents
    `ProgressDeadlineExceeded` from ever firing.** Found live while building
    the `failed-rollout` lab scenario (Milestone 8, `lab/manifests/scenario-
    failed-rollout/deployment.yaml`): the first draft reused the `crashloop`
    scenario's exact container (exits 1 after a 2s sleep, no probe defined) with
    a short `progressDeadlineSeconds: 15`, expecting the rollout to never
    progress and the controller to eventually give up. Instead,
    `kubectl get deployment -o json` showed `Progressing=True,
    reason=NewReplicaSetAvailable` -- the rollout was considered successful.
    Root cause: a container with no `readinessProbe` is marked `Ready=true` the
    instant it enters `Running`, with no check at all -- there was a real
    (sub-second) window where the Pod was genuinely `Ready` before its `sleep 2`
    elapsed and it crashed, and that window was enough for the Deployment
    controller to mark the new ReplicaSet available. `ProgressDeadlineExceeded`
    is therefore a correctness trap for a scenario author, not just an operator:
    a rollout that *looks* permanently broken (the Pod keeps crash-looping
    forever) can still read `Progressing=True` indefinitely if the crashing
    container was ever briefly `Ready`, because the Deployment controller only
    tracks "did the new ReplicaSet ever become available," not "is it available
    right now." Fixed by adding an always-failing `exec` `readinessProbe`
    (`/bin/false`) to the same container -- the same mechanism as the
    `readiness-probe-failure` scenario -- so the Pod is never considered
    `Ready`, not even for that brief window, guaranteeing the rollout genuinely
    cannot progress. Re-verified live: `kubectl wait
    --for=jsonpath='{.status.conditions[?(@.type=="Progressing")].reason}'=ProgressDeadlineExceeded`
    succeeded within the 15s deadline after the fix, where it had timed out
    at 40s before it.
20. **`RemediationService.propose()`'s per-incident action budget could be
    bypassed entirely by concurrent callers.** Found by the exact test
    Milestone 9 set out to write ("concurrent remediation requests", flagged
    as untested since TESTING.md was first written at Milestone 1) --
    `tests/integration/test_concurrent_remediation.py` fires 10 real threads
    at `propose()` for the same incident with `max_actions_per_incident=3`.
    Root cause: `propose()` reads `recent_actions` fresh from the repository,
    evaluates the policy decision against that snapshot, and only persists the
    result afterward -- with nothing serializing that read-decide-write
    sequence, N concurrent callers can all read "0 actions so far" before any
    of them has saved, and all independently decide the budget isn't exceeded
    yet. Confirmed failing exactly as predicted before any fix: all 10 threads
    were accepted (`PENDING_APPROVAL`), none rejected, against a budget of 3.
    Fixed with a `threading.Lock` on the `RemediationService` instance
    (`self._propose_lock`) guarding `propose()`'s entire read-decide-persist
    body -- a single process-wide lock is sufficient and proportionate for
    this project's single-process, local-tool design (the same reasoning
    already applied to `SQLiteIncidentRepository`'s own lock), not a
    distributed-systems-grade fix for a tool that explicitly isn't one. Re-ran
    the same test after the fix: exactly 3 accepted, 7 correctly rejected,
    every single time. **Known, deliberately out-of-scope remainder**: this
    lock only protects `propose()` against other `propose()` calls -- it does
    not protect `approve()`/`execute()`/`reject()` against racing each other
    (or against a concurrent `propose()`) on the *same* incident object held by
    two different callers, which could still silently lose one caller's status
    change via `repo.save()`'s whole-row overwrite. Closing that fully would
    need either per-incident locking or a DB-level optimistic-concurrency
    token (e.g. an `updated_at`/version check on save) -- a reasonable
    direction for future work, not something this milestone claims to have
    fixed.
21. **README.md's own documented remediation CLI commands would have failed if
    actually run.** Found live during Milestone 10's documentation pass,
    specifically by re-running every documented command rather than trusting
    it still matched the code (the spec's own "validate all documented
    commands" requirement, taken literally): `kubesentinel dry-run
    <incident-id> restart_deployment` fails outright --
    `ActionType`'s CLI argument matches an enum member by its literal
    (uppercase) value, not case-insensitively, so the lowercase form
    README.md had shown since Milestone 6 was never actually executable.
    Separately, the `approve` example was missing the required
    `--approved-by` flag entirely. Both are now corrected in README.md and
    docs/DEMO_SCRIPT.md, and the corrected sequence (`RESTART_DEPLOYMENT`
    against the real `failed-rollout-demo` incident, through
    `dry-run -> plan -> approve -> execute`) was re-run live end to end to
    confirm it actually works -- including confirming the honest
    `VERIFICATION_FAILED` outcome `execute` correctly reports, since a restart
    was never going to fix an always-failing readiness probe. See
    TROUBLESHOOTING.md.

## Environment note (not a KubeSentinel limitation): shared-VM resource
contention during this session's Milestone 11 verification

While live-verifying Milestone 11, `./scripts/test.sh --all`'s e2e/integration
run showed real flakiness (TLS handshake timeouts, a `kube-apiserver`
self-restart, a `SCALE_DEPLOYMENT` that took over 90s instead of the usual
~5-10s) that looked, at first, like a KubeSentinel or test-design bug. Root
cause, confirmed via `docker stats`: this machine's Colima VM was running
*two* separate kind clusters simultaneously -- this project's
`kubesentinel-control-plane` and an unrelated `ai-platform-engineer-control-
plane` -- with the second one alone using 259% CPU and 51% of the VM's total
memory. The combined demand across both clusters plus a Jenkins container
exceeded what the shared VM could actually provide. This is ordinary resource
contention between two independent local dev environments sharing one VM, not
a defect in this project; every test that showed flakiness had already passed
cleanly earlier in this same session (and does again once contention eases).
Not something to "fix" here -- noted so a future reader doesn't mistake
environment noise for a regression. If this recurs, stop the unrelated
cluster/VM workload first before re-running `--all`, rather than chasing it as
a KubeSentinel bug.

## Milestone 25: Milestone 1 audit closure -- DONE

Per the user's own explicit instruction ("first tackle those 3 and then
proceed to next milestone"): the 3 named, optional gaps left open by
Milestone 20's own repository audit (recorded in
`docs/PLATFORM_ROADMAP.md`'s Milestone 1 section) after Milestones 21-24
(scheduling, network, capacity, security) shipped. All bug-fix/hardening
items against existing scope, not a new capability -- no new design doc,
per the user's own scoping.

**Item 1 -- `UsageSummary.throttled` scaffolded but never populated**:
implemented, not removed. `optimization/analyzer.py::_check_throttled()`
runs the real `container_cpu_cfs_throttled_seconds_total` Prometheus
`increase(...[5m])` query and returns `True`/`False`/`None` -- never
coerced to `False` on query failure or metrics-server-only evidence, the
same "never fabricate absence" rule this project applies everywhere else.
A confirmed `True` now vetoes a proposed CPU decrease exactly the way a
recorded OOM kill already vetoes one for memory, and adds a
`CPU_THROTTLED_CONFIRMED` flag with its own reasoning/risk text; a
confirmed `False` states that fact explicitly instead of staying silent.
Verified with 5 new unit tests (`test_analyzer.py::TestCpuThrottling`)
against a mocked `urllib.request.urlopen`, the same technique the
existing Prometheus-path tests already use, extended from two queries
(cpu, memory) to three. Not live-verified against a real Prometheus
server, deliberately: Prometheus has never been part of this project's
own lab infrastructure and remains an optional, disabled-by-default
provider -- the unit-test tier is the correct and sufficient one here,
consistent with every other Prometheus-dependent path in this codebase.

**Item 2 -- no in-Pod mount-failure diagnostic rule**: implemented.
`diagnostics/rules/mount.py::FailedMountRule` fires HIGH confidence on
any Warning Event with `reason == "FailedMount"`, agnostic to the
underlying cause (unreachable backing store, multi-attach conflict,
wrong `fsType`, missing ConfigMap/Secret reference, etc.) -- it names the
symptom, not the specific cause, the same discipline
`PvcBindingFailureRule` already established for the separate
binding-failure case. Added to `diagnostics/engine.py::DEFAULT_RULES`
(positioned after `PvcBindingFailureRule`, before `NodeConditionRule`).
New lab scenario `scenario-failed-mount` (a missing-ConfigMap volume
reference -- confirmed live with a throwaway probe Pod, before writing
the manifest, that this reliably produces a real `FailedMount` Event;
this lab's own kind-on-local-path setup has no real external storage
infrastructure to break deterministically, so this is honestly not the
PVC-with-unreachable-storage case that originally motivated the rule).
4 new unit tests (`test_mount_rules.py`).

**Two real bugs caught live during this item's own verification, not by
any unit test** -- confirming, yet again, why this project insists on
live verification through the real pipeline rather than trusting
hand-built evidence alone:

1. `incidents/health.py::_BAD_EVENT_REASONS` needed `"FailedMount"` added
   -- without it, a Pod stuck this way would never open an incident at
   all (no container waiting/terminated state reflects this failure
   mode; the Event is the only evidence it produces). Caught by reasoning
   about `has_failure_signal()`'s role before writing any code, not live
   -- but recorded here because it was a required companion change, not
   optional polish.
2. `observer/service.py::_RELEVANT_EVENT_REASONS` -- a **second**,
   independent Event-reason allowlist, upstream of and separate from the
   one above, gating whether the real `EventWatcher` forwards the Event
   to `IncidentManager.observe()` at all. This one was caught only by
   live verification: the one-shot `diagnose` CLI command structurally
   cannot exercise either rule (it never fetches Events, only
   `pod_evidence()`), so a throwaway demo API server was started against
   the real `scenario-failed-mount` Pod to exercise the real
   `ObserverService` end to end. The real `FailedMount` Event fired
   correctly, but `GET /incidents` showed no Pod-level incident at all --
   root-caused to this second filter list silently dropping the Event
   before `_BAD_EVENT_REASONS` was ever consulted. Fixed, with a new
   regression test (`test_observer_service.py::test_failed_mount_event_is_forwarded`)
   and reconfirmed live immediately after: the real Pod-level incident
   now opens and is diagnosed `FAILED_MOUNT` at HIGH confidence end to
   end, and both `tests/integration/test_mount_rule_live.py` tests pass
   against the real cluster.

**Item 3 -- no cross-link from a resource-optimization finding back into
an open incident's own hypothesis list**: implemented, deliberately
narrow for its first case. `Hypothesis.related_recommendation_ids` (new
field, default empty list) is populated by the new
`IncidentManager._enrich_with_recommendations`, called from
`_apply_diagnosis` right after `DiagnosticEngine.diagnose()` returns --
never from inside a `DiagnosticRule` itself, since the engine must stay a
pure function of evidence with zero repository access (this project's own
"deterministic first" invariant). Only applies to an `OOM_KILLED` top
hypothesis (the roadmap's own named example) against open `MEMORY`/
`DATA_QUALITY` recommendations for the same workload, resolved via
`incident.resource.owner_name or incident.resource.name`.
`IncidentManager.__init__` gained an optional `optimization_repo`
parameter -- additive; every existing caller passing only `repo` behaves
exactly as before (confirmed by a dedicated unit test). `api/app.py`'s
lifespan was reordered so `state.optimization_repo` is constructed before
`state.manager`, so the real API server actually wires this up. 6 new
unit tests (`test_incident_manager.py::TestRecommendationCrossLink`)
against a real `IncidentManager`, real `Hypothesis`/`Incident` model
objects, and a real (on-disk SQLite) `SQLiteOptimizationRepository` --
covering both target categories, the no-match case, the non-OOM
hypothesis case, the no-`optimization_repo`-wired-up safe-no-op case, and
the different-workload case.

A full live demonstration against the lab's own `oom-demo` scenario was
attempted and deliberately not forced through: blocked by two independent,
pre-existing environmental facts, neither introduced by nor fixable within
this change. (a) This kind cluster's container runtime reports plain
`Error`, never `OOMKilled`, for this workload's real kills (confirmed via
`kubectl get pod ... -o json`, exit_code=137 under reason `Error`), so
`OOMKilledRule` only ever produces its MEDIUM-confidence *inferred*
hypothesis here (already documented as an accepted limitation in
`oomkilled.py`'s own docstring) -- which loses the real incident's top-
hypothesis ranking to the HIGH-confidence `CrashLoopBackOffRule` hypothesis
on the same Pod, confirmed live via `GET /incidents/{id}` showing both
hypotheses present, `crash_loop_backoff` HIGH ranked above `oom_killed`
MEDIUM. (b) A real `kubesentinel optimize --namespace kubesentinel-lab`
pass against the live lab produced 3 recommendations, zero of them for
`oom-demo` -- a perpetually crash-looping Pod never holds a long enough
live window for metrics-server to return a usage sample, so the optimizer
has nothing to analyze for that workload at all. Given both, the unit-test
tier is the correct and sufficient verification here, for the same reason
given for item 1: there is no live path to exercise that doesn't itself
depend on fixing unrelated, already-correct, already-documented behavior
elsewhere in the system.

**Tests**: 15 new unit tests total (5 + 4 + 6) across 3 files, plus 1
regression test (`test_observer_service.py`) for the live-caught bug in
item 2 -- 16 new unit tests altogether. 2 new live integration tests
(`test_mount_rule_live.py`). `./scripts/test.sh`: ruff clean, mypy clean
(98 source files), **731 unit tests passed** (715 + 16). `./scripts/test.sh
--all`: collected **800 tests** (731 unit + 69 integration/e2e). A full
`--all` run hit the same shared-VM-contention pattern documented
repeatedly throughout this project's history (36 failed, 4 errored --
RBAC-boundary `kubectl auth can-i` subprocess flakiness, network/capacity/
optimization live-test flakiness, one e2e remediation-lifecycle timeout;
both new `test_mount_rule_live.py` tests were themselves caught in this
same contention wave, despite having passed cleanly in isolation minutes
earlier); every one of the 40 affected tests passed cleanly (56 passed,
re-running the full affected files) when re-run immediately afterward
once `kube-scheduler`/`kube-controller-manager` stopped crash-looping.
This is at least the tenth confirmation of this exact environmental
pattern across this project's history, using the identical
"isolate and re-run" method each time.

**Documentation**: `docs/PLATFORM_ROADMAP.md` (Milestone 1 section
rewritten, all 3 items marked resolved with the live-caught-bug detail
for item 2 and the honest blocked-live-path accounting for item 3),
`docs/IMPLEMENTATION_STATUS.md` (CPU throttling row, PVC/mount row, the
Capability A/B cross-link row), `docs/RESOURCE_OPTIMIZATION_DESIGN.md`
(CPU throttling implemented note), `docs/DIAGNOSTIC_RULES.md`
(`FailedMountRule` section, including the second live-caught bug),
`docs/FAILURE_LAB.md` (new `scenario-failed-mount` section),
`docs/TESTING.md` (updated counts, new Milestone 1 closure test section,
the live-test bug-catch note), and `README.md` (the diagnostic-category
list now includes in-Pod volume mount failures).

Immediately after, per the user's own standing instruction, proceeds to
Milestone 7 ("Chaos and resilience lab") per `docs/PLATFORM_ROADMAP.md`'s
own sequencing.

## Milestone 26: Chaos & resilience evaluation harness -- DONE

Milestone 7 of `docs/PLATFORM_ROADMAP.md`, per the user's own standing
instruction to continue there after Milestone 25's closure work. Design-
first, per this project's established precedent:
`docs/CHAOS_RESILIENCE_LAB.md` before any code -- including the genuine
architectural decision this capability needed that no prior milestone
did: this is the project's first capability that doesn't read the
cluster to find a new problem, it *drives and times* the six engines that
already exist, so none of `MASTER_ARCHITECTURE.md`'s "one pattern" quite
applies as written. Stated explicitly in the design doc rather than
silently bent to fit.

**What was added**:

- `evaluation/scenarios.py` -- a hardcoded `ScenarioSpec` registry of all
  16 lab scenarios, each entry transcribing that scenario's own manifest
  comment (every one already states its own "Expected ... diagnosis" --
  confirmed by reading all 16 before writing the registry) as machine-
  readable ground truth. Deliberately not a new YAML schema or a comment
  parser -- see docs/CHAOS_RESILIENCE_LAB.md §2 for why either would mean
  two sources of truth drifting apart. `pending-resources` is registered
  twice (TROUBLESHOOTING and SCHEDULING), reusing the one manifest the lab
  already shares between those two engines' own live verification, rather
  than inventing a second manifest the roadmap's own scope said not to
  add.
- `evaluation/harness.py::EvaluationHarness` -- the orchestrator. Applies/
  cleans up a scenario via a `kubectl` subprocess (reusing exactly what
  `lab/scripts/run-scenario.sh`/`cleanup.sh` already do, under the
  ambient kubeconfig -- lab tooling, never through `ClusterClients`, so no
  new RBAC grant was needed anywhere). Every run deletes-then-confirms-
  absent before applying, so time-to-detection is measured from a genuine
  verified-zero baseline regardless of whatever stale state the shared,
  long-lived lab namespace happened to be in. Dispatches per scenario to
  one of two measurement strategies: a harness-owned, short-lived
  `ObserverService`+`IncidentManager` for the push-based troubleshooting
  engine (reusing `ObserverService` wholesale, no watch logic duplicated);
  re-triggering `OptimizationService.analyze()` /
  `SchedulingService.analyze_pod()` / `NetworkService.analyze_pod()` /
  `SecurityService.audit()` directly in-process each poll attempt for the
  five pull-based engines, the same construction `cli/main.py`'s own
  `_build_*_service` helpers already use. Recovery time is read straight
  off `Incident.recovery_duration_seconds` and is `None` for every
  scenario except `transient-recovery` -- the only one that defines a
  recovery path at all; no other scenario's "recovery" is fabricated or
  silently assumed. False-positive rate is computed once per whole battery
  (`_count_false_positives`): every currently-open incident/finding across
  all five repositories that cannot be attributed to any registered
  scenario's own resource name is counted as unattributed.
- `models/evaluation.py` (`EvaluationCapability`, `DetectionOutcome`,
  `ScenarioResult`, `EvaluationRun`) and a sixth sibling repository,
  `EvaluationRepository`/`SQLiteEvaluationRepository` behind
  `persistence/interface.py` -- unlike `SecurityRepository`'s whole-run
  replace, `save_run` is an upsert by `run_id` and every past run is kept:
  an evaluation run's entire purpose is comparing against history, not
  describing only the latest cluster state.
- `api/evaluation_routes.py` -- `GET /evaluation/runs[/{run_id}]` only.
  Deliberately **no `POST /evaluation/run`**: unlike `/optimization/
  analyze` or `/security/audit`, running a battery mutates the shared lab
  namespace and can take several minutes -- this stays CLI-only
  (`kubesentinel evaluate`/`evaluation-runs`/`evaluation-run`), the same
  boundary that already keeps `lab/scripts/*.sh` out of the HTTP surface
  entirely.

**A real bug caught live, not by any unit test**: a single scenario can
open *more than one* real incident whose resource names share the same
prefix -- `scenario-failed-mount` opens both a Deployment-level
`UNSATISFIED_REPLICAS` incident (the Deployment's own name exactly equals
`resource_name_prefix`) and the real Pod-level `FAILED_MOUNT` one (the
Pod's randomly-suffixed name also starts with that prefix). The harness's
first version took whichever the repository happened to list first the
instant either one existed, reporting a false `WRONG_CATEGORY` for a
scenario that was actually correctly diagnosed moments later. Root cause
confirmed from a live run's own JSON output, not assumed. Fixed in
`_check_troubleshooting` to keep polling every candidate for an exact
match against `expected` until the deadline, only settling for the
first-seen non-matching candidate once time genuinely runs out (for an
honest `WRONG_CATEGORY`/`TIMED_OUT` detail) -- reconfirmed live
immediately after with an identical run.

**Live-verified against the real cluster**: a bounded, representative
run -- one scenario per capability (`failed-mount`, `missing-requests`,
`pending-resources-scheduling-fit`, `security-findings`,
`service-port-mismatch`), not the full 16-scenario battery, deliberately:
a full run is disruptive to the shared lab (every scenario touched gets
torn down and recreated) and dominated by `oversized-resources`'s own
400s floor (it needs real metrics-server history to accumulate, per its
own manifest). All 5 reported `DETECTED` with the correct observed value
after the fix above -- `failed-mount` -> `FAILED_MOUNT`, `missing-
requests` -> `DATA_QUALITY`, `pending-resources-scheduling-fit` ->
`UNSCHEDULABLE`, `security-findings` -> `PRIVILEGED_CONTAINER`,
`service-port-mismatch` -> `CONFIGURATION_ERROR`. The same shared-VM
contention pattern documented repeatedly throughout this project's
history (`kube-apiserver`/`kube-scheduler`/`kube-controller-manager`
crash-looping from the two unrelated containers on this host) blocked the
cluster mid-verification twice; both times, waited for `kubectl get
nodes` to succeed again before retrying, per this project's own
established discipline, rather than working around it.

A full `./scripts/test.sh --all` run afterward showed 29 failed/4 errored
-- re-running those files in isolation once the host settled confirmed 59
of 60 were this exact same contention pattern, passing cleanly. The one
exception, `test_hpa_capacity_pressure_scenario_is_flagged_at_max`, kept
failing even in isolation -- investigated rather than dismissed as more
contention noise, since it's a different *kind* of failure (a wrong flag
value, not a connection timeout). Root cause, confirmed via
`kubectl -n kube-system get pods -l k8s-app=metrics-server`: the real
`metrics-server` Pod has been in `CrashLoopBackOff` for over 24 hours on
this specific host tonight (172 restarts) -- the same shared-VM
contention, just starving a *different* Pod than usual.
`capacity.py`'s own `HPA_AT_MAX` flag correctly requires real, observed
over-target utilization data before claiming a workload is pinned at its
ceiling under load (`optimization/capacity.py:254-256`); with no
metrics-server data at all, there is honestly no such evidence, so the
code falls through to the `elif` branch and reports `HPA_AT_MIN` instead
(`current_replicas <= min_replicas`, trivially true here since this
scenario's own `minReplicas == maxReplicas == 1` by design -- see its own
manifest comment). This is the *same* "honest about absence" discipline
this project applies everywhere else working exactly as intended under a
genuinely data-less condition, not a defect in `capacity.py`, and nothing
in this milestone's own code touches that logic at all -- no fix needed
or made here.

**Tests**: 24 new unit tests across 4 files -- `test_evaluation_scenarios.py`
(8, registry integrity), `test_evaluation_repo.py` (4, SQLite roundtrip/
ordering/upsert), `test_evaluation_harness.py` (10, `_scenario_label` and
the whole-battery false-positive computation against real model objects),
`test_evaluation_api.py` (4, the two read-only routes' happy path/empty/
404). `./scripts/test.sh`: ruff clean, mypy clean (104 source files),
**755 unit tests passed** (731 + 24).

**Documentation**: `docs/CHAOS_RESILIENCE_LAB.md` (new design doc),
`docs/PLATFORM_ROADMAP.md` (Milestone 7 marked DONE),
`docs/IMPLEMENTATION_STATUS.md` (Controlled Chaos & Resilience Lab:
Partial -> Implemented), `docs/TESTING.md` (updated counts, new Milestone
7 test-coverage section), and `README.md` (a new paragraph alongside the
other five engines, plus the documentation index entry).

## Milestone 27: Kubernetes operator -- DONE

Milestone 8 of `docs/PLATFORM_ROADMAP.md`, per the user's own standing
instruction ("Continue straight into Milestone 8") after Milestone 26's
evaluation harness shipped. Design-first:
`docs/OPERATOR_DESIGN.md` before any code -- this milestone's own
genuine architectural question, worked through before writing a line:
whether to grant the operator any new decision-making authority at all.
Reading `remediation/service.py::RemediationService.propose()` and
`policy/engine.py`'s own comments confirmed this project has never built
reusable logic for "which `ActionType` fixes this diagnosis" -- a human
always supplies it explicitly, and every rule through this milestone
still sets `hypothesis.auto_remediation_permitted=False` on every
hypothesis it produces. Building that mapping now, inside "a thin
reconciliation controller," would have been real, new, nontrivial
decision logic -- exactly the overreach `MASTER_ARCHITECTURE.md`'s
policy-boundary section warns against for any new apply path. Scoped the
controller down instead to something narrower and fully supported by
code that already exists: re-evaluating an already-proposed, still-
`PENDING_APPROVAL` action against a CR's declared policy, auto-approving
only what the existing `PolicyEngine` already allows.

**One CRD, not two**: `KubeSentinelPolicy` had no concrete content left
to carry once `KubeSentinelRemediationPolicy` existed -- the only policy
*gate* this project has ever built is `PolicyEngine`, and every other
capability's findings are explicitly not policy-gated at all
(`MASTER_ARCHITECTURE.md`'s own "Policy boundary" section). Scoped down
to one, per the roadmap's own explicit permission to do so.

**No `kopf`/`kubebuilder` dependency**: confirmed live, before writing
any watcher code, that `CustomObjectsApi` already returns a plain `dict`
from both its list and watch calls (no typed model exists for any CRD) --
the exact shape `observer/watchers.py::EndpointSliceWatcher` already
handles for an unrelated reason. Added a third mode to the shared
`_ListWatchLoop` (`dict_object_watch`, alongside the existing
`raw_object_watch` for EndpointSlice's own typed-model bug) and a
seventh, thin `CustomResourceWatcher` -- reusing this project's own
proven list-then-watch/backoff/resync loop wholesale rather than bridging
`kopf`'s asyncio runtime into this project's synchronous, threading-based
one for exactly one watcher.

**What was added**:

- `crds/kubesentinelremediationpolicy.yaml` -- the CRD, namespaced,
  group `kubesentinel.io`, version `v1alpha1`, with a `status`
  subresource. Every `spec` field mirrors an existing `Settings` field
  1:1 (`mode`/`policy_mode`, `cooldownSeconds`/`action_cooldown_seconds`,
  etc.) -- a namespace-scoped, declarative way to set values that were
  previously only global and environment-variable-configured, not a new
  policy concept.
- `models/operator.py` -- `RemediationPolicySpec`/`RemediationPolicyRef`,
  typed parsing for the CR's own dict shape (defensive even though the
  CRD's own OpenAPI schema already rejects most malformed specs at
  admission time).
- `observer/watchers.py` -- `dict_object_watch` mode on `_ListWatchLoop`
  and the new `CustomResourceWatcher` class, generic over any future
  namespaced CRD's `group`/`version`/`plural`, not hardcoded to this
  one.
- `operator/controller.py::RemediationPolicyController.reconcile()` --
  for a given CR: build a scoped `Settings` copy from its spec (never a
  cross-process config push to an already-running API server -- the
  operator is its own independent reader of the same cluster and
  database, exactly like a CLI invocation); list this namespace's
  incidents with a `PENDING_APPROVAL` action; re-read each target
  Deployment's current `spec.replicas` live (never trust a stale value);
  re-build a `PolicyRequest` from the action's and incident's own
  already-recorded fields and call `PolicyEngine.evaluate()` fresh; if
  now `ALLOWED`, call `RemediationService.approve()` then `.execute()` --
  the identical two calls a human makes, just performed automatically,
  auditable as `approved_by="kubesentinel-operator"`; patch the CR's own
  `.status` (never policy-gated -- it's bookkeeping on the operator's own
  object, not a remediation action).
- `operator/service.py::OperatorService` -- wires `CustomResourceWatcher`
  to the controller, the operator's own equivalent of
  `observer/service.py::ObserverService`.
- `cli/main.py` -- `kubesentinel operator-run`, a new blocking command
  (like `uvicorn` blocks for the API server), not a second executable --
  keeps "the one CLI" convention.
- `rbac/operator-role.yaml`/`operator-rolebinding.yaml` -- namespaced,
  additive to the existing Roles bound to the same `kubesentinel`
  ServiceAccount, granting only what's new (read/watch the CRD, patch its
  own status subresource). The actual remediation mutation reuses
  `remediation-role.yaml` unchanged -- the operator performs the exact
  same cluster-mutating calls a human-approved action already does, so
  it needs exactly the same permissions, not a new or broader set.

**A real bug caught by this milestone's own unit tests, before it ever
reached the live cluster**: re-evaluating an already-persisted pending
action built its `recent_actions` list via the same
`list_actions_since()` call `propose()` uses -- but unlike `propose()`
(which builds that list *before* the new action is ever saved), the
action under re-evaluation is already sitting in the repository, so it
appeared in its own `recent_actions` and self-denied on a 0-second-old
cooldown against itself. `propose()` was never wrong; this was a new
failure mode specific to re-evaluating something already persisted.
Fixed in `_maybe_auto_approve` by excluding the action's own `action_id`
from `recent_actions` before building the `PolicyRequest`, with an inline
comment explaining why `propose()` itself never needed this.

**Live-verified against the real cluster**: RBAC confirmed against the
real constrained `kubesentinel` ServiceAccount (not admin) for both the
CRD and its status subresource (`kubectl auth can-i ... --as=...`) --
including a brief false alarm from this agent's own `kubectl auth can-i`
invocation syntax (combining the CRD's API-group suffix with `/status`
doesn't parse the way `--subresource=status` does), caught and corrected
before being reported as a gap. A real `crashloop-demo` incident (the
Deployment-level `UNSATISFIED_REPLICAS` one, not the Pod-level
`CRASH_LOOP_BACKOFF` sibling -- `RESTART_DEPLOYMENT` only targets
Deployments) had a real action proposed and left `PENDING_APPROVAL`
under `APPROVAL_REQUIRED`. Applying a real `KubeSentinelRemediationPolicy`
CR with `mode: CONTROLLED_AUTO` and running the real `operator-run`
process against it confirmed the action was correctly left alone (its
diagnosing rule, `unsatisfied_replicas`, forbids auto-remediation like
every rule in this codebase today) -- the CR's own `.status` showed
`pendingActionsSeen: 1, actionsAutoApproved: 0`. Since no rule in this
project's existing diagnostic engine ever sets
`auto_remediation_permitted=True` (a deliberate, conservative, already-
shipped decision, not a gap -- see `policy/engine.py`'s own comment),
demonstrating genuine auto-approval end to end needed manually marking a
copy of the same incident's hypothesis eligible and temporarily scaling
the target Deployment to 2 replicas (restored to 1 immediately
afterward) to clear the single-replica safety check -- disclosed here
plainly as a synthetic override of this project's real rule set, not
something to be mistaken for a rule this project's diagnostics actually
produce today. With that override in place, the same real operator
process correctly auto-approved and executed a real Deployment restart:
`kubectl.kubernetes.io/restartedAt` confirmed updated on the real
Deployment's pod template, `approved_by: kubesentinel-operator` recorded
in the incident's own audit trail, and an honest
`VERIFICATION_INCONCLUSIVE` final status -- a restart never fixes this
scenario's deterministic crash, so a false "recovered" was never
reported. The lab was restored to its original state (1 replica, no
leftover CR) immediately after; the CRD and RBAC remain installed as
the real, permanent product artifacts this milestone ships.

**Tests**: 19 new unit tests across 3 files --
`test_operator_models.py` (6), `test_operator_controller.py` (9, where
the cooldown-self-denial bug above was actually caught),
`test_custom_resource_watcher.py` (4). `./scripts/test.sh`: ruff clean,
mypy clean (108 source files), **774 unit tests passed** (755 + 19).

**Documentation**: `docs/OPERATOR_DESIGN.md` (new design doc),
`docs/PLATFORM_ROADMAP.md` (Milestone 8 marked DONE),
`docs/IMPLEMENTATION_STATUS.md` (Kubernetes-native Operator and
Reconciliation Controller: Missing -> Implemented), `docs/TESTING.md`
(updated counts, new Milestone 8 test-coverage section), and `README.md`
(a new paragraph alongside the evaluation harness's own, plus the
documentation index entry).

A full `./scripts/test.sh --all` run afterward showed all 69
integration/e2e tests SKIPPED rather than run -- investigated rather than
accepted at face value: the cluster was completely unreachable
(`HTTPSConnectionPool... Read timed out`) for the entire fixture-setup
phase, the same shared-VM contention pattern documented repeatedly
throughout this project's history, this time manifesting as a clean skip
(by the `live_cluster_clients` fixture's own design) rather than a hard
failure. Re-running `tests/integration tests/e2e` directly once the
cluster recovered showed the same familiar 29 failed/4 errored
contention-class set; re-running those files in isolation again brought
it down to 3 genuine failures, each independently root-caused rather
than dismissed: `test_hpa_capacity_pressure_scenario_is_flagged_at_max`
(the same metrics-server `CrashLoopBackOff` root cause already recorded
in Milestone 26's own entry), `test_observer_still_detects_after_pod_deleted_and_recreated_with_same_name`
(skipped, not failed, on its own next retry -- a timing-sensitive live
precondition not met at that exact instant), and
`test_inventory_matches_known_lab_workloads` (expected `remediation-test`
to show 2 ready replicas, found 3, with only 2 real Pods actually
running -- confirmed live via `kubectl get deployment remediation-test -o
wide` showing a stale `3/2` status, and `kubectl get pods -n kube-system`
showing `kube-controller-manager` itself in `CrashLoopBackOff` at that
exact moment: the one controller that reconciles a Deployment's own
`.status` subresource wasn't reliably running, leaving stale bookkeeping
behind -- not a Milestone 8 regression, and not a product bug at all).
None of the three touch anything Milestone 8 added.

This completes every milestone in `docs/PLATFORM_ROADMAP.md` except
Milestone 9 (final integration and portfolio demonstration).

## Milestone 28: Final integration and portfolio demonstration -- DONE

Milestone 9 of `docs/PLATFORM_ROADMAP.md`, the last one, per the user's
own standing instruction ("Continue straight into Milestone 9") after
Milestone 27's operator shipped. Scope per the roadmap: a full regression
suite, a fresh local setup verification from a clean clone, and a
coherent end-to-end demo spanning every capability.

**Fresh clone verification, done for real, not read through**: `git
clone` of this exact working tree into a scratch directory (not the real
GitHub remote -- no unpushed commits were pushed as part of this), then
ran, in order: `./scripts/check-prereqs.sh` (all green), `./scripts/setup.sh`
(fresh `.venv`, fresh `pip install -e ".[dev]"`, fresh `.env` from
`.env.example`), the fresh venv's own `./scripts/test.sh` (**774 passed**,
confirming dependency pinning and packaging are correct from a cold
clone, not just this session's already-populated `.venv`), and
`./lab/create-cluster.sh` against the already-running cluster -- read
`create-cluster.sh` first to confirm it's genuinely idempotent (`kind`
no-ops if the cluster exists, `kubectl apply` is idempotent) before
running it, rather than risk destroying 2+ days of accumulated lab state
on an assumption. The fresh clone's own CLI binary was then run directly
against the real cluster (`incidents`, and `--help` on `review-recommendation`,
`dry-run`, `plan`, `schedule-check`, `network-check`, `evaluate`),
confirming every argument/flag README documents is the argument/flag the
CLI actually accepts.

**Real documentation drift found and fixed** -- not the kind the roadmap
specifically named (a misleading flag, a truncated ID), but the same
underlying failure mode in a different shape: *coverage* drift. Milestones
7 and 8 (the evaluation harness, the operator) existed fully in code and
in their own dedicated design docs, but had never been added to
`README.md`'s Quickstart section, its Project layout tree, or
`docs/DEMO_SCRIPT.md` at all -- three separate places a reader would
reasonably expect to find them and wouldn't. Also found:
`DEMO_SCRIPT.md`'s own step 9 stated "There's no AI layer yet (Milestone
11, optional)" -- flatly wrong, since AI summarization (`kubesentinel
explain`, `/incidents/{id}/ai-summary`) has shipped and is documented
elsewhere in the very same file (step 6's sibling content, README's own
Quickstart). Fixed all of it:

- `README.md` -- Quickstart gained `evaluate`/`evaluation-runs` and
  `operator-run` command examples (previously absent entirely); Project
  layout gained `evaluation/`, `operator/`, and `crds/` entries, and
  updated the `persistence/`/`api/`/`cli/`/`rbac/` lines to mention the
  sixth repository, the new routes, and the operator's own Role; the
  documentation index line for `PLATFORM_ROADMAP.md` now says "everything
  except Milestone 9 itself" instead of stopping at "Capabilities A-F."
- `docs/DEMO_SCRIPT.md` -- new steps **8g** (evaluation harness) and
  **8h** (operator), both written and then run live against the real
  cluster, command by command, to confirm they work exactly as written
  before committing the doc -- not assumed, the same discipline this
  entire project applies everywhere else. The stale AI line in step 9 is
  now accurate, and step 9 additionally states the operator's own
  boundary (it never decides which action to propose, only auto-approves
  one already proposed) alongside the pre-existing "nothing calls
  propose() automatically" sentence, which remained true and needed no
  change. The intro line's "matching what's implemented through
  Milestone 18" was replaced with a description of the actual, current,
  complete scope.
- `docs/PLATFORM_ROADMAP.md` -- Milestone 9 marked DONE with this
  evidence.

**Live verification of the new doc sections themselves**: ran
`kubesentinel evaluate --scenario failed-mount --json` exactly as step
8g now documents -- confirmed `time_to_detection_seconds` and
`time_to_diagnosis_seconds` really do read the same value (the push-based
architectural fact step 8g claims) and `recovery_time_seconds` is really
`null` for this scenario; ran `evaluation-runs`/`evaluation-run <id>` and
confirmed the table/detail output matches what's described. Applied the
real CRD and a real CR exactly as step 8h documents, ran
`operator-run`, confirmed the CR's `.status` shows the same shape
documented (`pendingActionsSeen`/`actionsAutoApproved`/`lastReconcileTime`/
`message`), then ran the new Cleanup section's own commands
(`kubectl delete ksrp ...`, `kubectl delete -f crds/...`) and confirmed
they actually remove what they claim to.

**Full regression suite**: `./scripts/test.sh` -- ruff clean, mypy clean,
**774 unit tests passed**, both in this session's own `.venv` and in the
fresh clone's independently-built one. `./scripts/test.sh --all`, run as
the final, official record for this milestone, took 16m46s (nearly
double the usual ~500s) and showed 49 failed/4 errored -- the worst single
instance of the shared-VM-contention pattern documented repeatedly
throughout this project's history, confirmed from the run's own first
error: a raw `SSLEOFError` against the API server mid-test, not an
application-level failure. Re-running every affected file together in
isolation (`tests/e2e/test_full_remediation_lifecycle.py`,
`test_capacity_live.py`, `test_mount_rule_live.py`, `test_network_live.py`,
`test_optimization_inventory_live.py`, `test_optimization_live_scenarios.py`,
`test_rbac_boundary.py`, `test_restart_recovery.py`,
`test_scheduling_live.py`, `test_security_live.py`, `test_watch_resync.py`,
`test_observer_live_detection.py`) brought it down to exactly 2 failures,
each independently resolved rather than waved through:
`test_propose_approve_execute_verify_scale_up_and_back_down` (a real
scale-up genuinely took longer than its 30s verification deadline under
contention -- re-ran alone and passed cleanly in 13.73s, confirming
transient timing, not a defect) and
`test_hpa_capacity_pressure_scenario_is_flagged_at_max` (the same,
already-documented `metrics-server` `CrashLoopBackOff` root cause from
Milestone 26's own entry, reconfirmed still true: `kubectl get pods -n
kube-system -l k8s-app=metrics-server` showed 196 restarts at the time).
Neither touches anything this milestone, or Milestones 7/8, added.

**Documentation**: `README.md` (Quickstart, Project layout, documentation
index), `docs/DEMO_SCRIPT.md` (new steps 8g/8h, fixed stale AI line,
fixed stale intro milestone reference, updated Cleanup section),
`docs/PLATFORM_ROADMAP.md` (Milestone 9 marked DONE). No change was
needed to `docs/MASTER_ARCHITECTURE.md`'s "proven six times" line --
checked deliberately, not assumed safe to skip -- it is still correct as
written: it counts only the six Capability A-F analysis engines
specifically, and both the evaluation harness and the operator are
already, correctly, documented in their own design docs as deliberate
exceptions to that one pattern, not a seventh and eighth instance of it.

This completes every milestone in `docs/PLATFORM_ROADMAP.md`, including
Milestone 9 itself -- the full platform specification (Capabilities A-F,
the chaos & resilience evaluation harness, and the Kubernetes operator)
is now built, tested, live-verified, and documented.

## Milestone 29: Close the Pod-UID-dedup-survives-a-rollout limitation -- DONE

Not a platform-roadmap milestone -- the oldest open item in this file's
own "Known limitations" section (Milestone 1's own entry, #1): "Pod-UID
dedup doesn't survive a Deployment rollout." A rollout replaces a Pod
under a new UID; the old Pod's incident never receives another
observation at all and sits open forever. Asked about, and by the user's
own explicit follow-up request, written up as a real design first
(`docs/INCIDENT_RECONCILIATION_DESIGN.md`) before any code -- the user
then asked to implement it.

**What "fixed" had to mean, precisely**: never claim a recovery that was
never observed. A Pod replaced by a rollout is never going to report
healthy again; there is no later observation to confirm one. The design's
load-bearing constraint, carried through to the implementation exactly:
a new terminal `IncidentStatus.SUPERSEDED`, distinct from `RESOLVED`,
which never sets `recovery_duration_seconds`.

**What was added**:

- `models/incident.py` -- `IncidentStatus.SUPERSEDED`.
- `incidents/manager.py` -- `IncidentManager` gained an optional
  `clients: ClusterClients | None = None` constructor parameter
  (additive; every existing caller passing only `repo` is unaffected).
  `_maybe_supersede_stale_pod_incidents()` fires on *every* confirmed-
  healthy Deployment observation (not gated on "an incident just
  resolved" -- the design's own key insight: a clean rollout that never
  opens its own Deployment-level incident is the common case, and that
  gate would have silently skipped it). A cheap, local repository query
  decides whether there's anything to sweep at all before any live API
  call happens. For each candidate: look up its recorded `owner_uid`
  (the ReplicaSet at observation time) in a live, namespace-wide
  ReplicaSet list by UID, confirm that ReplicaSet's own
  `ownerReferences` names the exact Deployment that was just observed
  healthy (by `uid`, never by name), then confirm the Pod itself is
  genuinely gone via a live 404 -- only then call the new `_supersede()`,
  which mirrors `_resolve()`'s own shape exactly except it never touches
  `recovery_duration_seconds` or its histogram.
- `optimization/ownership.py::controller_ref` -- promoted from a private
  `_controller_ref`, reused rather than duplicated: the exact same
  Pod -> ReplicaSet -> Deployment ownership-chain logic
  `resolve_pod_owner()` already used for an unrelated capability is the
  only correct way to confirm a ReplicaSet's own controller without
  guessing from a name prefix (this project's own established refusal to
  use fragile string heuristics for a correctness-sensitive decision).
- `persistence/sqlite_repo.py::find_open_by_resource_uid`,
  `api/app.py::cluster_summary`, `evaluation/harness.py`'s own false-
  positive counter, and `IncidentManager.reconcile_metrics()` -- every
  existing "is this incident still open" check across the codebase was
  updated to also exclude `SUPERSEDED`, not just `RESOLVED`; found by a
  deliberate, exhaustive grep for every such check before writing any
  test, not assumed safe to skip.
- `api/app.py`'s lifespan was reordered so `clients` is built (or safely
  set to `None` on failure, exactly preserving today's
  "serve read endpoints without a cluster" behavior) before
  `IncidentManager` is constructed, the same restructuring shape already
  used once for `optimization_repo` (Milestone 1's own audit closure,
  item 3) -- and `evaluation/harness.py`'s own `IncidentManager`
  construction was updated to pass its already-available `clients` too,
  for consistency.

**Tests**: 10 new unit tests in `test_incident_manager.py`'s new
`TestSupersedeStalePodIncidents` class -- covers the safe-no-op cases (no
`clients` wired up, no candidates at all), the positive case (confirmed
gone + confirmed healthy parent -> superseded, with
`recovery_duration_seconds` asserted `None` and the audit entry asserted
exactly), and every negative case named in the design's own "what this
doesn't attempt" section (a pruned ReplicaSet, a ReplicaSet belonging to
a different Deployment, the Pod still existing, an inconclusive non-404
live check, an already-RESOLVED/SUPERSEDED incident never reconsidered)
plus the one case proving the design's own central claim -- the sweep
fires even when the Deployment itself never opens an incident of its
own. `tests/unit/factories.py::make_replica_set` gained a `uid` parameter
(additive; every existing call site keeps its old default). All 10
passed on their first run -- stated plainly, not dressed up as a bug
hunt that didn't happen this time.

**Live-verified against the real cluster**, with a throwaway crash-
looping Deployment rather than any permanent `lab/manifests/` scenario
(every one of those is relied on by some other milestone's own live test
to *stay* broken, so none was a safe "fix it and watch" candidate here).
A real Pod-level `CRASH_LOOP_BACKOFF` incident opened; the Deployment was
patched to a command that actually succeeds (a genuine rollout -- new
ReplicaSet, new Pod, old Pod terminated); once the Deployment reported
healthy, the old Pod's incident was confirmed precisely: `status:
SUPERSEDED`, `resolved_at` set, `recovery_duration_seconds: None`, and
the final audit entry naming `replaced_by_deployment: "supersede-test-
demo"` and the exact live-404-plus-healthy-parent reason. The
Deployment's own incident resolved through the unchanged, ordinary
`RESOLVED` path alongside it, with a real measured
`recovery_duration_seconds` (221.4s) -- confirming the new sweep didn't
disturb the path it runs next to. Full detail, including the precise
field dump, is in `docs/INCIDENT_RECONCILIATION_DESIGN.md`'s own new
"Implemented and live-verified" section.

Two environmental complications were hit during this one live-
verification run, both self-resolved and neither a code defect: this
host's chronic shared-VM contention (documented repeatedly throughout
this project's history) showed up twice -- once as
`kube-controller-manager` stuck retrying with a literal `"too many open
files"` error for several minutes before a ReplicaSet could even be
created, and again as a second `kube-controller-manager`/
`kube-scheduler` `CrashLoopBackOff` episode that delayed the rollout
itself. Both were waited out patiently, never worked around. Separately,
and worth recording honestly rather than quietly fixing and moving on:
this same verification pass discovered several *stale* throwaway demo-
server processes left running on other ports from earlier, unrelated
points in this same session -- an operational cleanup slip by this
agent, not a finding about the feature itself. All were found and killed
before the verification concluded.

**Full regression suite**: `./scripts/test.sh` -- ruff clean, mypy clean
(108 source files), **784 unit tests passed** (774 + 10).

**Documentation**: `docs/INCIDENT_RECONCILIATION_DESIGN.md` (the design
doc itself, plus its new "Implemented and live-verified" section).
`TODO.md`'s own "Known limitations" entry for this item is left in place
unedited -- it accurately describes what the gap *was*; this new
milestone entry is where its closure is recorded, matching this
project's own "TODO.md is append-only history, not a doc that gets
rewritten" discipline.

## Milestone 30: Effective RBAC permission resolution -- DONE

The first of five named, deliberate scope boundaries the user asked to
close, in the order agreed: safest/most in-pattern first. Closes
`docs/SECURITY_AUDITING.md` Section 12's own long-named gap: the two
existing RBAC rules (`ClusterAdminBindingRule`, `WildcardRbacRuleRule`)
each look at one binding or one rule at a time, never a subject's
combined access across every binding naming it. Written up as a new
Section 13 in that same doc (an extension of an already-designed
capability, not a new standalone design doc) before any code.

**Scoped narrowly, on purpose**: the tempting, broader version --
"does this subject's combined rule set amount to cluster-admin-
equivalent access" -- needs judgment calls this project has no
principled way to make deterministically. Kubernetes itself doesn't
leave the *one* specific case to judgment: the `escalate` and `bind`
verbs on `roles`/`clusterroles` are the two verbs its own RBAC admission
treats as explicitly bypassing the built-in self-escalation prevention
check. A subject holding either, from any combination of its bindings,
can grant itself or anyone else arbitrary additional permissions later
-- the one case this first pass evaluates, deliberately not a broader
heuristic.

**What was added**:

- `security/rbac_resolution.py` (new) -- pure functions over an
  already-built `SecurityInventory`, no new API calls: resolves every
  RoleBinding/ClusterRoleBinding's `roleRef` to the real Role/ClusterRole
  it names (a RoleBinding may reference either a Role in its own
  namespace or a ClusterRole, both real Kubernetes patterns; an
  unresolvable reference is skipped, never guessed at), grouped by every
  subject each binding names.
- `security/rules/rbac.py::EffectiveRbacEscalationRule` (new,
  `rule_id="RBAC_SELF_ESCALATION_CAPABLE"`) -- unions the resolved rules
  across every one of a subject's bindings (one or several; a
  single-binding case is exactly as real a finding as a multi-binding
  one, so neither is treated as the only interesting case), checks for
  `escalate`/`bind` on `roles`/`clusterroles`, excludes built-in
  Roles/ClusterRoles (reusing `WildcardRbacRuleRule`'s own exclusion,
  promoted to a shared `_is_builtin_role_name` function -- the same
  cluster control-plane subjects legitimately need this verb and are
  already excluded there for the same reason), and names every
  contributing binding in its own evidence so a multi-binding case reads
  differently from a single-binding one.
- `security/engine.py::default_rules` -- registered the new rule.

**Tests**: 19 new unit tests -- `test_rbac_resolution.py` (11, the pure
resolution/matching functions) and a new `TestEffectiveRbacEscalationRule`
class in `test_security_rbac_rules.py` (8, including the multi-binding
evidence-assembly case and both builtin exclusions). One existing test
(`test_security_engine.py::test_default_rules_includes_every_documented_rule_id`)
needed its own expected rule-id set updated -- found immediately by
running the suite, not missed.

**Live-verified against the real cluster**: a real ServiceAccount bound
via a real RoleBinding to a real Role granting `escalate` on `roles` in
the lab namespace produced exactly the expected `RBAC_SELF_ESCALATION_CAPABLE`
CRITICAL finding, with accurate evidence naming the real binding/Role.
Separately, and just as important: confirmed the exclusion correctly
holds against a *real* system ClusterRole that genuinely has this verb
today -- `system:controller:clusterrole-aggregation-controller` grants
`escalate` on `clusterroles` (confirmed via `kubectl get clusterroles -o
json`), and the real audit run against the whole cluster correctly never
flagged it. Test fixture removed immediately after.

**Documentation**: `docs/SECURITY_AUDITING.md` (new Section 13, Section
12 updated to point to it rather than left describing a now-closed gap),
`docs/IMPLEMENTATION_STATUS.md` and `README.md` (rule count: 13 -> 14).
`docs/PLATFORM_ROADMAP.md`'s own Milestone 6 section is left unedited --
it's a historical record of what that milestone shipped with (13 rules
at the time), and this 14th rule was added afterward as separate,
explicitly-requested follow-up work, not part of that milestone.

**Full regression suite**: ruff clean, mypy clean, **803 unit tests
passed** (784 + 19).

## Milestone 31: Pod affinity/anti-affinity and topology spread evaluation -- DONE

The second of the five scope boundaries the user asked to close, per
the same "safest/most in-pattern first" order as Milestone 30 above.
Closes `docs/SCHEDULING_ANALYSIS.md` Section 2's own named gap: pod
affinity/anti-affinity and topology spread constraints were detected
and reported, never actually evaluated, because doing so needs every
other already-scheduled Pod's labels across every candidate node --
more state than the existing per-node checks track. Scoped and written
up in that same design doc before any code (Section 2's "evaluated for
real" list, and the `ConstraintType` data model in Section 4).

**Scope decisions, made deliberately rather than assumed**:
- Only the *required* half of pod affinity/anti-affinity is evaluated
  (`REQUIRED_POD_AFFINITY`/`REQUIRED_POD_ANTI_AFFINITY`, new
  `ConstraintType` values) -- the preferred half stays a soft scoring
  preference, same treatment `PREFERRED_NODE_AFFINITY` already has.
- Required pod-affinity/anti-affinity terms are **ANDed** (per real
  Kubernetes semantics for `[]PodAffinityTerm`), unlike node affinity's
  own OR'd `nodeSelectorTerms` -- verified against the Kubernetes docs
  before writing the evaluator, not assumed by analogy.
- A required term using `namespaceSelector` can't be evaluated (would
  need a separate per-Namespace-labels API call, out of scope) and is
  never guessed at: an affinity term that can't be proven met is
  **not satisfied** (never claim a required condition holds without
  evidence); an anti-affinity term that can't be proven *violated* is
  **satisfied** (never fabricate a problem it hasn't earned the
  evidence for). The same unevaluatable term is also surfaced in
  `unevaluated_constraints` either way, so a caller knows the
  required-side result may be incomplete.
- `TOPOLOGY_SPREAD` has no required/preferred split (`whenUnsatisfiable`
  is per-constraint, not a separate list) -- every
  `topologySpreadConstraint` is now evaluated for real, with
  `ScheduleAnyway` treated as soft/non-blocking, mirroring the existing
  `PreferNoSchedule` taint precedent.
- `minDomains`, `matchLabelKeys`, `nodeAffinityPolicy`,
  `nodeTaintsPolicy` (newer, less commonly set topology-spread fields)
  are not evaluated -- a documented v1 simplification, not an oversight.

**What was added**:

- `models/scheduling.py` -- `REQUIRED_POD_AFFINITY`/
  `REQUIRED_POD_ANTI_AFFINITY` added to `ConstraintType` and
  `EVALUATED_CONSTRAINT_TYPES`; `TOPOLOGY_SPREAD` moved into the
  evaluated set; `POD_AFFINITY`/`POD_ANTI_AFFINITY`'s own meaning
  refined (not renamed) to mean specifically "the preferred half, or a
  required term this engine can't evaluate" -- never the required
  terms that can. New `PodLabelSummary` model (uid/namespace/node_name/
  labels -- the minimum these checks need about one other Pod).
- `scheduling/node_inventory.py::build_pod_label_inventory()` (new) --
  a second, separate `list_pod_for_all_namespaces()` snapshot, kept
  apart from `build_node_inventory()` (used by 3 callers, pinned by 10
  existing tests) rather than changing that function's return shape and
  forcing `optimization/service.py` to receive and discard data it has
  no use for.
- `scheduling/constraints.py` -- `label_selector_matches()` (promoted
  from `security/rules/availability.py::_pdb_selector_matches`, now a
  third caller; standard "null matches nothing, empty matches
  everything" LabelSelector semantics -- the *opposite* of
  `network/policy_inventory.py`'s own deliberately-different
  NetworkPolicy `podSelector` handling, never to be confused), plus
  `check_pod_affinity`, `check_pod_anti_affinity`, and
  `check_topology_spread_constraints` (full skew calculation: per-domain
  Pod counts across every Node's own topology value, the candidate Pod
  hypothetically added to its own domain, checked against `maxSkew`).
- `scheduling/analyzer.py` -- the three new checks wired into
  `analyze_pod_scheduling`'s per-node loop; `_detect_unevaluated_constraints`
  rewritten so a required pod-affinity/anti-affinity term the engine
  *can* evaluate is never also reported as unevaluated (only the
  preferred half, or a `namespaceSelector` term, is); new `pods`/
  `pod_uid` parameters.
- `scheduling/service.py` -- both `analyze_pod` and `analyze_hypothetical`
  now call `build_pod_label_inventory()` and pass it through, with
  `pod_uid` set to the real Pod's own UID for `analyze_pod` (so it
  excludes itself from matching against its own labels) and `None` for
  a hypothetical spec (nothing to exclude).
- `security/rules/availability.py` -- its own local
  `_pdb_selector_matches` removed in favor of importing the promoted
  `label_selector_matches`, no behavior change.

**Tests**: 24 new unit tests across `test_scheduling_constraints.py`
(`TestLabelSelectorMatches`, `TestCheckPodAffinity`,
`TestCheckPodAntiAffinity`, `TestCheckTopologySpreadConstraints` --
including the namespaceSelector conservative-asymmetry case in both
directions, ANDed multiple terms, the candidate Pod excluded from
matching itself, and topology-spread skew both within and exceeding
`maxSkew` for both `DoNotSchedule` and `ScheduleAnyway`) and
`test_scheduling_node_inventory.py` (`TestBuildPodLabelInventory`, 4).
Four existing tests in `test_scheduling_analyzer.py`'s
`TestUnevaluatedConstraintDetection` needed rewriting, not just
updating -- they pinned the *old* "always detected, never evaluated"
behavior this milestone deliberately changes; replaced with tests that
distinguish an evaluable required term (not in `unevaluated_constraints`
anymore) from a preferred entry or `namespaceSelector` term (still is).
The old `TestPdbSelectorMatches` class in
`test_security_availability_rule.py` (testing the now-deleted
`_pdb_selector_matches` directly) was removed; its 5 cases now live as
`TestLabelSelectorMatches` in `test_scheduling_constraints.py` instead,
testing the promoted public function in its new home.

**Live-verified against the real cluster**: created two real Pods in
`kubesentinel-lab` -- one plain Pod labeled `app=anti-affinity-demo`,
and a second with a required `podAntiAffinity` term against that same
label on `topologyKey=kubernetes.io/hostname`. On this lab's
single-node cluster, the real `kube-scheduler` genuinely left the
second Pod Pending with `FailedScheduling: 0/1 nodes are available: 1
node(s) didn't match pod anti-affinity rules`. `kubesentinel
schedule-check kubesentinel-lab anti-affinity-demo-b` independently
reached the same conclusion through its own math, not by reading the
scheduler's message: `REQUIRED_POD_ANTI_AFFINITY` reported
`satisfied: false`, `"found 1 matching Pod(s), expected none
(anti-affinity)"` -- naming the real conflicting Pod's own effect, in
the engine's own words, alongside (not instead of) the real scheduler
message it also carries. Test fixture deleted immediately after.
`REQUIRED_POD_AFFINITY`/`TOPOLOGY_SPREAD` were also exercised on real
data in the same run (both trivially satisfied -- no podAffinity or
topologySpreadConstraints on either demo Pod), confirming they run
without error against the real cluster even though this single-node
lab (`docs/SCHEDULING_ANALYSIS.md` Section 11) can't demonstrate a
topology-spread skew violation or a cross-node affinity match on its
own; that logic is covered by the unit tests above instead.

**Documentation**: `docs/SCHEDULING_ANALYSIS.md` (Section 2's "evaluated
for real" / "detected, not evaluated" bullets rewritten; Section 4's
`ConstraintType` snippet updated to match), `docs/IMPLEMENTATION_STATUS.md`
(Capability C table rows for pod affinity/anti-affinity and topology
spread updated to Implemented, split into required/preferred rows),
`README.md` (Capability C's own summary paragraph updated).
`docs/PLATFORM_ROADMAP.md`'s Milestone 3 section is left unedited, same
reasoning as Milestone 30's own precedent -- a historical record of
that milestone's original scope, with this closure recorded as
separate follow-up work here instead.

**No new RBAC**: `build_pod_label_inventory()` reuses the exact same
`clients.core.list_pod_for_all_namespaces()` call `build_node_inventory()`
already makes -- no new permission category introduced by this
milestone specifically.

**Full regression suite**: ruff clean, mypy clean, **828 unit tests
passed** (803 + 25, including 1 renamed/relocated from
`test_security_availability_rule.py`).

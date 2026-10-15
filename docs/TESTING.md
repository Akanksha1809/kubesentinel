# Testing

## What's actually run, and how to run it

```bash
./scripts/test.sh          # ruff + mypy + unit tests only (no cluster needed)
./scripts/test.sh --all    # same, plus integration/e2e against a live kind cluster
```

As of Milestone 11: **234 unit tests** (no cluster, no Ollama needed) plus **23
integration/e2e tests** (`--all`, against the real `kind-kubesentinel` cluster, one
of them additionally gated on a local Ollama server) -- 257 total, all passing,
`ruff check` clean, `mypy src/kubesentinel` clean (54 source files). Every
integration/e2e test skips (not fails) if its precondition (the cluster, the lab
namespace, a specific scenario/fixture, Ollama) isn't present -- confirmed by
running `--all` against a deliberately broken `KUBECONFIG`: all skipped cleanly,
zero errors. The combined suite was re-run four consecutive times with no failures
after fixing a flaky test found along the way (TODO.md's Milestone 9 entry has the
root cause -- a race in the test itself, not the product).

This was verified in-session, not assumed -- see the commands actually run, below.

## Unit test coverage (`tests/unit/`)

- **`test_diagnostic_rules.py`** -- every rule (`CrashLoopBackOffRule`,
  `ImagePullRule`, `OOMKilledRule` including its inferred-from-exit-code fallback and
  its negative cases), plus `DiagnosticEngine` ranking (healthy pod -> no hypotheses;
  OOM+CrashLoop co-occurrence ranks correctly by confidence).
- **`test_inspector.py`** -- evidence extraction from hand-built `V1Pod` objects
  (phase, waiting reason, last termination, missing status, owner reference
  extraction with and without an owner).
- **`test_health.py`** -- the unhealthy/healthy classifier, including the OOM-in-
  lastState-while-waiting-reason-is-CrashLoopBackOff case.
- **`test_incident_manager.py`** -- opening an incident, deduping repeated unhealthy
  observations into the same incident, resolving on recovery with a measured
  `recovery_duration_seconds`, and the UID-based identity guarantee: a new UID (even
  for the same name) opens a new incident, and a resolved incident doesn't block a
  fresh one for the same UID reappearing unhealthy.
- **`test_sqlite_repo.py`** -- save/get roundtrip, missing-id returns None,
  `find_open_by_resource_uid` excludes resolved incidents, save is an upsert (not a
  duplicate row), list filtering by status and namespace.
- **`test_backoff.py`** -- exponential growth, hard cap, rejects a negative attempt.
- **`test_observer_service.py`** -- DELETED events are ignored; a terminating Pod
  (`deletionTimestamp` set) is never fed into the incident manager even if its
  container statuses look transiently healthy; a normal healthy Pod *is* observed.

## Why `kubernetes.client` model objects, not hand-rolled dicts, in tests

`tests/unit/factories.py` builds `V1Pod`/`V1ContainerStatus`/etc. using the real
`kubernetes` client classes. This means a test exercises the exact attribute shapes
(`cs.state.waiting.reason`, not `cs["state"]["waiting"]["reason"]`) that
`inspector.py` reads against a live API response -- if the client library's shape
ever changed, these tests would catch a mismatch that a hand-rolled dict fixture
could not.

## Verified live against a real cluster (not mocked)

Beyond the automated suite, the full pipeline was run against an actual local kind
cluster in this session:

1. Created a kind cluster (`kind-kubesentinel`), applied the `kubesentinel-lab`
   namespace.
2. Applied the `crashloop`, `oom`, and `imagepull` Deployments plus the
   `transient-recovery` Pod.
3. Started the FastAPI server (`uvicorn kubesentinel.api.app:app`) pointed at that
   cluster with `KUBESENTINEL_WATCH_NAMESPACES=kubesentinel-lab`.
4. Confirmed via `GET /incidents`, `GET /incidents/{id}`, `GET /cluster/summary`,
   `GET /metrics`, and `kubesentinel incidents` (the installed CLI entry point) that:
   - All three permanent-failure scenarios were detected and correctly diagnosed
     (`CRASH_LOOP_BACKOFF`, `ERR_IMAGE_PULL`/`IMAGE_PULL_BACKOFF`, and the OOM
     scenario correctly carrying both the `CRASH_LOOP_BACKOFF` and inferred
     `OOM_KILLED` hypotheses, ranked by actual confidence).
   - The `transient-recovery` scenario opened an incident, then resolved it with a
     real measured `recovery_duration_seconds`, confirmed via the same API.
   - `/metrics` exposed correctly-labeled, bounded-cardinality counters/gauges for
     everything observed.
5. Found and fixed a real bug in the process (a terminating Pod's transient status
   could look like recovery) -- see TODO.md's "Known limitations" #2 and
   DESIGN_DECISIONS.md.

This matters because the spec explicitly asks that the final demo never replace real
Kubernetes API behavior with mocked data -- the above was run against the actual API
server of a real (if local) cluster, with real container runtime behavior (including
an environment-specific OOM-reporting quirk that wouldn't have shown up against a
mock).

## Integration tests (`tests/integration/`, Milestone 9)

All of these run a real `ObserverService`/`IncidentManager`/`RemediationService`
against the actual `kind-kubesentinel` cluster through a temporary SQLite file --
never a mock. `tests/conftest.py` and `tests/integration/conftest.py` hold the
shared live-cluster fixtures, every one of which skips (never fails) if its
precondition (a reachable cluster, the lab namespace, a specific scenario) isn't
met.

- **`test_rbac_boundary.py`** (16 tests) -- authenticates as the real
  `kubesentinel` ServiceAccount (a token minted live with `kubectl create
  token`, not fabricated) and confirms both what it can do (read Pods/Events/
  Deployments/Nodes in the lab namespace, update/patch Deployments, patch the
  scale subresource) and what it cannot (read Secrets anywhere, read Pods
  outside the lab namespace, delete Deployments/Pods/Nodes, mutate PVCs/
  Services) -- using both a real authenticated API call and `kubectl auth
  can-i --as=...` (the same technique used to manually confirm this boundary
  in Milestone 4).
- **`test_watch_resync.py`** -- runs a real `PodWatcher` against the live
  cluster with a 2s `resync_interval_seconds` and confirms, from real ADDED
  event timestamps, that the bounded relist safety net actually fires against
  a real API server (not just that the backoff math is correct, which
  `test_backoff.py` already covers). Does not exercise the HTTP 410 Gone path
  specifically -- that needs a resourceVersion to age out of the API server's
  cache, impractical to trigger quickly and deliberately left out rather than
  faked.
- **`test_observer_live_detection.py`** -- a real `ObserverService` opens a
  correctly-categorized incident for the real `readiness-probe-failure-demo`
  Pod, and a second test deletes that Pod and confirms the real recreated
  Pod (new UID) gets a *new* incident, never silently reusing the old one.
- **`test_restart_recovery.py`** -- the live counterpart to TODO.md #18: a
  simulated KubeSentinel restart (a fresh `IncidentManager`/`ObserverService`
  pointed at the same SQLite file) reuses the already-open incident's identity,
  and `reconcile_metrics()` makes `incidents_active` correct immediately, before
  the new process's own observer produces a single fresh detection.
- **`test_concurrent_remediation.py`** -- 10 real threads calling
  `RemediationService.propose()` concurrently on the same incident, confirming
  the per-incident action budget holds under a genuine race. This is the test
  that found TODO.md #20 (see below) -- it failed under the original code,
  letting all 10 through against a budget of 3.
- **`test_ai_summary_live.py`** (Milestone 11) -- a real call to a local
  Ollama server (not mocked), confirming `summarize_incident()` returns usable
  text end to end. Separately gated: skips specifically when Ollama isn't
  reachable, independent of the cluster-reachability skip every other test
  here uses -- a machine can have the cluster but not Ollama, or vice versa.

## E2E tests (`tests/e2e/`, Milestone 9)

- **`test_full_remediation_lifecycle.py`** -- the complete `propose -> approve
  -> execute -> verify` lifecycle, driven through `RemediationService` exactly
  as the API/CLI do, against the real `remediation-test` Deployment: scales it
  2 -> 3 (confirmed `VERIFIED_RECOVERED` against a fresh live read, not a
  trusted self-report) and back to 2, asserting the full `Incident.actions`/
  `audit_log` trail. This is the automated version of the manual scale/
  restart/rollback verification already documented in REMEDIATION_SAFETY.md.
  Always restores the Deployment to its documented 2/2 steady state, including
  on failure.

Two things TESTING.md originally scoped for Milestone 9 are deliberately not
covered: a full e2e run through *every* lab scenario (the one e2e test above
covers the remediation lifecycle end-to-end once, which is the part that
hadn't been automated yet; each diagnostic category already has live-verified
coverage per FAILURE_LAB.md), and the HTTP 410 Gone watch-invalidation path
specifically (noted above).

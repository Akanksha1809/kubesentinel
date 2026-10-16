# Testing

## What's actually run, and how to run it

```bash
./scripts/test.sh          # ruff + mypy + unit tests only (no cluster needed)
./scripts/test.sh --all    # same, plus integration/e2e against a live kind cluster
```

As of Milestone 7's own evaluation harness (after Milestone 1's audit
closure shipped): **755 unit tests** (no cluster, no Ollama needed) plus **69
integration/e2e tests** (`--all`, against the real `kind-kubesentinel` cluster, one
of them additionally gated on a local Ollama server) -- 824 total, all passing,
`ruff check` clean, `mypy src/kubesentinel` clean. Every
integration/e2e test skips (not fails) if its precondition (the cluster, the lab
namespace, a specific scenario/fixture, Ollama) isn't present -- confirmed by
running `--all` against a deliberately broken `KUBECONFIG`: all skipped cleanly,
zero errors. The combined suite was re-run four consecutive times with no failures
after fixing a flaky test found along the way (TODO.md's Milestone 9 entry has the
root cause -- a race in the test itself, not the product). The resource
optimization engine's own tests (Milestones 13-18) added 176 of the 410 unit
tests and 5 of the 32 integration/e2e tests (plus 4 of the RBAC boundary
suite's tests, specific to its own Role); the scheduling advisor's own
tests (Milestone 3 of the platform spec) added a further 92 unit tests and
4 integration/e2e tests (plus 3 more RBAC boundary tests); the network
troubleshooting engine's own tests (Milestone 4 of the platform spec) added
a further 64 unit tests and 5 integration/e2e tests (plus 7 more RBAC
boundary tests, covering its two new Roles); the capacity planning &
autoscaling advisor's own tests (Milestone 5 of the platform spec) added
a further 45 unit tests (41 in `test_capacity.py` plus 4 integration
tests added to `test_optimization_service.py`) and 3 integration tests
(plus 3 more RBAC boundary tests, covering its one new Role); the
security & configuration auditor's own tests (Milestone 6 of the
platform spec) added a further 104 unit tests and 6 integration tests
(plus 5 more RBAC boundary tests, covering its two new Roles); closing
Milestone 1's own three-item audit backlog (CPU throttling, in-Pod mount
failures, the optimization-to-incident cross-link) added a further 15
unit tests and 2 integration tests; see below.

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

## Resource optimization engine unit test coverage (Milestones 13-18)

- **`test_quantities.py`** (44 tests) -- CPU/memory quantity parse/format
  round-trips: bare integers, the `m` millicore suffix, decimal (`k`/`M`/`G`)
  vs. binary (`Ki`/`Mi`/`Gi`) memory suffixes, scientific notation, and
  malformed input raising `QuantityParseError` rather than silently
  defaulting to zero.
- **`test_ownership.py`** (10 tests) -- owner-reference chain resolution:
  Pod -> ReplicaSet -> Deployment (two hops), direct Pod -> StatefulSet/
  DaemonSet/Job, a standalone Pod (no owner), preferring the
  `controller: true` reference when more than one exists, and returning
  `None` (never guessing) for an unresolvable or unrecognized owner kind.
- **`test_inventory.py`** (16 tests) -- workload grouping across multiple
  replicas, the `status.readyReplicas`/`numberReady` `omitempty`-absence
  normalization (confirmed live against the real API, not assumed),
  recent-OOM detection, and unschedulable-reason extraction.
- **`test_metrics_providers.py`** (16 tests) -- `MetricsApiProvider`/
  `PrometheusProvider` against mocked HTTP responses (success, 404/missing,
  malformed data), and `InMemorySampleStore`'s bounded ring-buffer eviction.
- **`test_analyzer.py`** (23 tests) -- determinism (same input -> identical
  output), each safety-margin/percentile/floor parameter, the
  `EvidenceQuality` threshold gating any proposed number, CPU-vs-memory
  statistic asymmetry (percentile-of-typical vs. peak), OOM-history
  increasing scrutiny without auto-adjusting a limit, multi-replica usage
  pooling, the Prometheus upgrade path and its failure fallback, and
  `related_incident_ids` threading through to `ACTIVE_INCIDENT`.
- **`test_safety.py`** (11 tests) -- every flag
  (`UNHEALTHY_WORKLOAD`/`SYSTEM_NAMESPACE`/`JOB_CONSERVATIVE`/
  `DAEMONSET_CONSERVATIVE`/`ACTIVE_INCIDENT`) suppresses only the proposed
  number, never the row itself or its other flags; multiple flags stacking;
  pre-existing flags preserved, not overwritten.
- **`test_optimization_repo.py`** (13 tests) -- SQLite persistence
  round-trips for recommendations/workload snapshots/analysis runs, the
  single-row "latest analysis run" upsert pattern, and filtering.
- **`test_optimization_service.py`** (27 tests) -- supersession (a
  still-`NEW` prior recommendation expires, a reviewed one never does),
  effective-status computation (`EXPIRED` at read time, never stored),
  review state transitions and their error cases, incident-UID lookup
  (deduped, no-repo-wired-up is a safe no-op), and all 4 Prometheus metrics
  (`analysis_total`'s success/partial/failed labels, `recommendations_total`
  by category, `last_success_timestamp`, `metrics_data_available` by
  provider) using the same reset-then-assert `._value.get()` pattern as
  `test_incident_manager.py`'s `incidents_active` checks.
- **`test_optimization_api.py`** (16 tests) -- every route's happy path,
  404/409/422/503 error-code coverage (the 422/503 cases were added after
  finding both were missing against RESOURCE_OPTIMIZATION_DESIGN.md's own
  error-mapping table -- see TODO.md's Milestone 18 entry), and the
  token-gated review endpoint's 401 boundary.

## Scheduling advisor unit test coverage (Milestone 3 of the platform spec)

- **`test_scheduling_constraints.py`** (32 tests) -- every constraint
  function exhaustively: resource-fit arithmetic at and past the boundary,
  every taint effect (`NoSchedule`/`PreferNoSchedule`/`NoExecute`) crossed
  with every toleration operator (`Equal`/`Exists`), a soft
  `PreferNoSchedule` taint never masking a real blocking one alongside it,
  node-selector exact matching, all 6 real `NodeSelectorRequirement`
  operators (`In`/`NotIn`/`Exists`/`DoesNotExist`/`Gt`/`Lt`), multiple
  required-affinity terms OR'd vs. multiple expressions within one term
  AND'd, and namespace-quota exceeded/not-exceeded.
- **`test_scheduling_node_inventory.py`** (10 tests) -- requests summed
  only from Pods actually bound to a given Node, Pods on a *different* node
  or in a terminal phase (`Succeeded`/`Failed`) correctly excluded from that
  sum (the scheduler's own accounting), taints/labels/unschedulable/ready
  extraction, and that each Node gets its own independent total when
  multiple Nodes exist.
- **`test_scheduling_analyzer.py`** (19 tests) -- determinism (same input
  -> identical output, including per-constraint reasoning text), a cordoned
  or not-Ready Node never schedulable regardless of whether resources would
  otherwise fit, "schedulable if *any* node fits," the namespace-quota
  check's inclusion in `evaluated_constraints` only when explicitly
  requested, detection of every unevaluated constraint type (preferred node
  affinity, pod affinity, pod anti-affinity, topology spread) without
  attempting to evaluate any of them, and the hypothetical-spec path:
  deserializing a raw dict into a typed `V1PodSpec` and analyzing it
  identically to a real Pod's spec, including a namespace quota correctly
  blocking a hypothetical even when every Node would otherwise fit it.
- **`test_scheduling_repo.py`** (7 tests) -- SQLite save/get roundtrip,
  upsert-not-duplicate, namespace/schedulable filtering.
- **`test_scheduling_service.py`** (15 tests) -- a missing Pod raises
  `PodNotFoundError` (404-mapped), the namespace-quota check is *never*
  invoked for a real Pod and *always* invoked for a hypothetical spec
  (asserted via `assert_not_called()`/`assert_called_once()`, not just
  reading the output), the real `PodScheduled` condition's message is
  carried through, the `kubesentinel_scheduling_analysis_total` metric's
  three labels (schedulable/unschedulable/failed) using the same
  reset-then-assert pattern as the optimization engine's own metrics tests,
  and `related_incident_ids` linking (the no-repo-wired-up no-op, a match
  on the Pod's own UID, no match, and that the link survives persistence --
  added after a live check caught this field going unpopulated; see
  TODO.md's Milestone 21 entry).
- **`test_scheduling_api.py`** (9 tests) -- every route's happy path, 404
  (missing Pod/finding), 422 (malformed hypothetical spec -- deliberately
  distinct from 503, since a bad client input and an unreachable cluster
  are different failure classes; see `scheduling/service.py::InvalidPodSpecError`),
  503 (service unavailable), and namespace filtering on the findings list.

## Network troubleshooting engine unit test coverage (Milestone 4 of the platform spec)

- **`test_network_service_inventory.py`** (13 tests) -- Service
  selector-vs-Pod-label matching (exact equality, computed independently
  rather than inferred from EndpointSlice counts); the
  EndpointSlice-vs-selector consistency check across agreement and
  disagreement cases; `_resolve_target_port()`'s asymmetric numeric-vs-named
  semantics exhaustively (a numeric `targetPort` matching a declared port,
  matching no declared port but still structurally valid, and a named
  `targetPort` matching a declared port name vs. matching none at all).
- **`test_network_policy_inventory.py`** (9 tests) -- `podSelector`
  matching including the empty-selector-matches-*every*-Pod semantics
  (the opposite of a Service's own empty selector), `matchExpressions`
  evaluation via the reused `requirement_matches`, and the
  ingress/egress-rules-present-vs-absent summary text (deny-all vs.
  permits-matching-traffic).
- **`test_network_dns_health.py`** (6 tests) -- a fully healthy case, the
  `ready_replicas` omitempty-zero normalization (same bug shape already
  documented for `optimization/inventory.py`), partial CoreDNS readiness,
  a missing `kube-dns` Service, zero ready EndpointSlice endpoints, and an
  unreadable Deployment (RBAC/API failure) all correctly reported as
  unhealthy with a specific reason.
- **`test_network_analyzer.py`** (12 tests) -- every classification
  branch and the full priority ordering exhaustively (`DNS_UNHEALTHY` >
  `CONFIGURATION_ERROR` > `MISSING_OR_UNREADY_ENDPOINTS` >
  `POLICY_MAY_RESTRICT` > `HEALTHY`, with `INCONCLUSIVE` as its own
  no-selecting-Service branch), that a configuration error always outranks
  an applicable policy, that the "not a confirmed end-to-end connectivity
  test" caveat is always present in `reasoning`, determinism, and that
  workload fields are carried through unchanged.
- **`test_network_repo.py`** (7 tests) -- SQLite save/get roundtrip,
  upsert-not-duplicate, namespace/classification filtering.
- **`test_network_service.py`** (9 tests) -- a missing Pod raises
  `PodNotFoundError` (404-mapped), a DNS-unhealthy cluster state correctly
  drives the finding's own classification, `related_incident_ids` linking
  (the no-repo-wired-up no-op and a match on the Pod's own UID -- built in
  from the start this time, not found missing after the fact the way
  `SchedulingFinding` was), and the `kubesentinel_network_analysis_total`
  metric's `failed` label on a real exception alongside the happy path.
- **`test_network_api.py`** (8 tests) -- every route's happy path, 404
  (missing Pod/finding), 422 (an invalid `classification` filter value --
  a typed Enum query param from the start, applying Milestone 18's own
  lesson rather than rediscovering it), 503 (service unavailable), and
  namespace filtering on the findings list.

## Capacity planning & autoscaling advisor unit test coverage (Milestone 5 of the platform spec)

- **`test_capacity.py`** (41 tests) across six test classes:
  `TestHpaToSnapshot` (converting a real `V2HorizontalPodAutoscaler` into
  an `HpaSnapshot`, including the `ScalingLimited` condition read
  verbatim), `TestBuildHpaInventory`/`TestAttachHpaSnapshots` (the
  all-namespaces vs. per-namespace LIST path; matching an HPA to a
  `WorkloadSnapshot` by scale-target kind+name, including a kind
  mismatch correctly *not* matching), `TestComputeTrend` (every
  `TrendDirection` branch, including the zero-baseline edge case where a
  naive percentage-change calculation would divide by zero),
  `TestDetectBottleneckPattern` (the single-threaded-ceiling pattern
  firing only with enough plateaued replicas *and* a sufficiently higher
  limit, and explicitly not firing when usage is merely spread out
  across a wide range rather than clustered), `TestComputeScaleHeadroom`
  (fits/doesn't-fit/exactly-at-the-boundary cases, unschedulable/
  not-Ready Nodes excluded, and that *cumulative* multi-replica headroom
  is checked correctly, not just a single replica's worth), and
  `TestAnalyzeCapacity` (every branch end to end: no finding when
  nothing fires, a healthy HPA's status-only finding, `HPA_AT_MAX` with
  and without `SCALING_WOULD_BE_UNSCHEDULABLE` depending on real
  headroom, `HPA_AT_MIN`, `MISSING_REQUESTS_FOR_AUTOSCALING_MATH`, the
  memory-leak pattern firing/not-firing depending on window length and
  whether CPU is also increasing, the single-threaded-bottleneck pattern
  firing without any HPA at all, the "not a statistical forecast" caveat
  always present, and determinism).
- **`test_optimization_service.py`**'s new `TestCapacityIntegration`
  class (4 tests) -- confirms `OptimizationService.analyze()` actually
  wires `build_hpa_inventory()`/`attach_hpa_snapshots()`/
  `analyze_capacity()` together correctly (an HPA targeting a workload
  produces a persisted `CAPACITY` recommendation; no HPA produces none;
  a capacity finding goes through the exact same generic
  `safety.py::apply_safety_checks` every other category already does,
  confirmed via `ACTIVE_INCIDENT`; and that a Node-inventory read failure
  degrades the one headroom check gracefully rather than aborting the
  entire `analyze()` pass).

## Security & configuration auditor unit test coverage (Milestone 6 of the platform spec)

- **`test_security_container_rules.py`** (31 tests) -- all 8 container
  rules exhaustively: each rule's fire/don't-fire cases, `RunAsRootRule`'s
  Pod-level-fallback-with-container-override precedence, `HostNamespace
  SharingRule` firing once (not three times) for three shared namespaces
  at once, and `_has_mutable_tag()` on its own across six cases including
  a digest-pinned reference and a registry host with a port (so the
  `:5000` isn't mistaken for a tag).
- **`test_security_rbac_rules.py`** (14 tests) -- `ClusterAdminBindingRule`
  across both binding kinds (cluster-wide CRITICAL vs. namespace-scoped
  HIGH), mixed subjects on one binding only flagging the non-built-in
  one, and both real built-in Group exclusions (`system:masters` and
  `kubeadm:cluster-admins` -- the second found live, see below);
  `WildcardRbacRuleRule` across verb+resource and verb+apiGroup
  combinations, a wildcard verb alone *not* firing, and both the
  exact-name and `system:`-prefix built-in-Role exclusions.
- **`test_security_network_policy_rules.py`** (7 tests) -- namespace
  coverage with/without a policy and with the system-namespace exclusion;
  unrestricted-ingress firing/not-firing including the deny-all
  (zero-rules) and egress-only cases, reusing
  `network/policy_inventory.py`'s own classification.
- **`test_security_availability_rule.py`** (11 tests) -- `_pdb_selector_matches()`
  on its own (the standard null-matches-nothing/empty-matches-everything
  semantics, deliberately the *opposite* of NetworkPolicy's own
  podSelector handling) plus the full rule across covered/uncovered/
  single-replica/wrong-namespace/wrong-selector cases for both
  Deployment and StatefulSet.
- **`test_security_engine.py`** (7 tests) -- `SecurityAuditEngine`
  collecting findings from every rule, `rules_evaluated` listing every
  rule ID, inventory `warnings` propagating to the result, determinism,
  and `default_rules()` including every one of the 13 documented rule IDs
  (a pin against silently dropping a rule from the catalog).
- **`test_security_inventory.py`** (6 tests) -- Pod-to-workload dedup via
  a real resolvable owner chain, `namespaces_with_pods` tracking, a
  ClusterRoleBinding-list failure recorded as a warning without affecting
  any other read, and the all-namespaces-vs-per-namespace fanout.
- **`test_security_repo.py`** (11 tests) -- including the real bug this
  milestone caught live: `test_save_findings_replaces_the_entire_previous_set`
  pins that a second `save_findings()` call replaces, never appends to,
  the first run's rows.
- **`test_security_service.py`** (9 tests) -- including
  `test_second_audit_does_not_accumulate_stale_findings`, the
  service-level version of the same regression, plus the
  `kubesentinel_security_audit_total`/`kubesentinel_security_findings_total`
  metrics.
- **`test_security_api.py`** (8 tests) -- every route's happy path, 404
  (missing finding), 422 (an invalid `severity` filter value), 503
  (service unavailable), and namespace/rule_id filtering on the findings
  list.

## Milestone 1 audit closure unit test coverage

Three named, optional gaps from the original Milestone 0 audit, picked up
after Milestones 2-6 shipped:

- **`test_analyzer.py`'s new `TestCpuThrottling` class** (5 tests) --
  confirmed-`True`, confirmed-`False`, a query failure leaving `throttled`
  `None` (never coerced to `False`), the `CPU_THROTTLED_CONFIRMED` flag/
  reasoning on a near-limit finding, and that confirmed throttling vetoes
  a proposed CPU decrease the same way a recorded OOM kill already vetoes
  one for memory. All five via the same mocked-`urllib.request.urlopen`
  technique the existing Prometheus tests already use, extended from two
  queries (cpu, memory) to three (cpu, memory, throttle).
- **`test_mount_rules.py`** (4 tests) -- `FailedMountRule` fires on a
  `FailedMount` Event and not on an unrelated one, that the Event is a
  real `has_failure_signal` (without which no incident would ever open
  for this failure mode), and that `supporting_evidence` references the
  real Event's own id.
- **`test_observer_service.py::test_failed_mount_event_is_forwarded`**
  (1 test) -- regression test for a real bug caught by live verification,
  not by any unit test: `observer/service.py::_RELEVANT_EVENT_REASONS` is a
  second, independent Event-reason filter, upstream of and separate from
  `incidents/health.py::_BAD_EVENT_REASONS`, that also needed `"FailedMount"`
  added -- otherwise the real `EventWatcher` silently dropped the Event
  before `IncidentManager.observe()` ever ran, so `FailedMountRule` could
  never fire against a real cluster despite being itself correct.
- **`test_incident_manager.py`'s new `TestRecommendationCrossLink` class**
  (6 tests) -- an `OOM_KILLED` hypothesis links to a `MEMORY` recommendation
  and separately to a `DATA_QUALITY` one, no match leaves the list empty,
  a different reason code (`CRASH_LOOP_BACKOFF`) is never enriched even
  with a matching recommendation present, no `optimization_repo` wired up
  is a safe no-op (every existing caller passing only `repo` must behave
  exactly as before), and a recommendation for a *different* workload is
  correctly not linked.

## Milestone 7 evaluation harness unit test coverage

The harness's own orchestration (applying manifests via `kubectl`, driving
a real `ObserverService`/`OptimizationService`/etc. against a real
cluster) is deliberately not re-mocked at the unit tier, and deliberately
not wired into the automated `--all` suite either: a battery run deletes-
then-reapplies every scenario it touches in the shared lab namespace
(docs/CHAOS_RESILIENCE_LAB.md §6), which is disruptive, developer-invoked
behavior, not something routine CI should silently repeat. It was instead
verified directly below ("Live"). 24 new unit tests cover what *is*
purely deterministic:

- **`test_evaluation_scenarios.py`** (8 tests) -- registry integrity: every
  scenario name is unique, every `lab_dir` exists on disk, every
  SCHEDULING/NETWORK scenario declares the pod label it needs,
  `transient-recovery` is the only scenario with `supports_recovery=True`,
  `oversized-resources` carries a long timeout, and `pending-resources`/
  `pending-resources-scheduling-fit` correctly share one manifest across
  two capabilities.
- **`test_evaluation_repo.py`** (4 tests) -- `SQLiteEvaluationRepository`
  save/get/list roundtrips, ordering by `started_at` descending, and that
  `save_run` is an upsert by `run_id` (unlike `SecurityRepository`'s
  whole-run replace, an evaluation run's entire purpose is comparing
  against *past* runs, so history must be kept -- see
  docs/CHAOS_RESILIENCE_LAB.md §7).
- **`test_evaluation_harness.py`** (10 tests) -- `_scenario_label()`'s
  prefix-stripping, and `_count_false_positives()` (the whole-battery
  false-positive computation) against real `Incident`/
  `ResourceRecommendation`/`SchedulingFinding`/`NetworkFinding`/
  `SecurityFinding` objects: a known resource is never a false positive, an
  unattributable one (e.g. a real CoreDNS Pod name) is, a `RESOLVED`
  incident is excluded entirely, a Scheduling/Network finding can match on
  either of its two name fields, a `SecurityFinding` with no
  `workload_name` at all (RBAC/NetworkPolicy/PDB findings genuinely have
  none) is honestly counted as unattributed rather than silently excluded,
  and an empty cluster state returns `(0, 0)` rather than a division error.
- **`test_evaluation_api.py`** (4 tests) -- `GET /evaluation/runs[/{id}]`'s
  happy path, empty-list case, and 404; no `POST` route exists to test
  (docs/CHAOS_RESILIENCE_LAB.md §8).

**Live**: a bounded, representative run (one scenario per capability --
`failed-mount`, `missing-requests`, `pending-resources-scheduling-fit`,
`security-findings`, `service-port-mismatch`) via `kubesentinel evaluate`
against the real cluster, not the full 16-scenario battery (disruptive to
the shared lab and dominated by `oversized-resources`'s own 400s floor).
All 5 correctly `DETECTED`, after catching and fixing a real bug live: the
first version matched an incident by resource-name prefix alone, which
for `scenario-failed-mount` matches *two* real incidents (a Deployment-
level `UNSATISFIED_REPLICAS` one and the real Pod-level `FAILED_MOUNT`
one) -- it took whichever the repository listed first, reporting a false
`WRONG_CATEGORY`. Fixed to keep polling for an exact match against
`expected` until the deadline; reconfirmed live immediately after.

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

- **`test_rbac_boundary.py`** (38 tests) -- authenticates as the real
  `kubesentinel` ServiceAccount (a token minted live with `kubectl create
  token`, not fabricated) and confirms both what it can do (read Pods/Events/
  Deployments/Nodes/StatefulSets/DaemonSets/Jobs/ResourceQuotas/pod-metrics/
  Services/NetworkPolicies/HorizontalPodAutoscalers/ClusterRoles/
  ClusterRoleBindings/Roles/RoleBindings/PodDisruptionBudgets in the lab
  namespace (or cluster-wide for the cluster-scoped RBAC objects), update/
  patch Deployments, patch the scale subresource) and what it cannot
  (read Secrets anywhere, read Pods outside the lab namespace, delete
  Deployments/Pods/Nodes, mutate PVCs/Services/NetworkPolicies/
  HorizontalPodAutoscalers/ClusterRoles/ClusterRoleBindings/Roles/
  RoleBindings/PodDisruptionBudgets, patch
  StatefulSets/DaemonSets/Jobs/ReplicaSets/ResourceQuotas) -- using both a
  real authenticated API call and `kubectl auth can-i --as=...` (the same
  technique used to manually confirm this boundary in Milestone 4).
  The `metrics.k8s.io` pods check is deliberately *not* asserted via
  `can-i` -- found live (Milestone 18) that `can-i` answers `no` for this
  aggregated API even when the Role genuinely grants it, so that one check
  goes through a real authenticated request instead (treating a `503` --
  metrics-server itself down -- as inconclusive, not a failure, since
  that's an availability question, not an RBAC one). Seven of the 38 cover
  the network engine's two new Roles specifically, including its
  `kube-system` exception: `kubectl_can_i()` gained an optional
  `resource_name` parameter (`TYPE/NAME` syntax) because that grant is
  scoped via `resourceNames` to exactly `coredns`/`kube-dns` by name --
  plain `can-i` without a resource name reports `no` for every one of
  those rules regardless of whether the grant exists, the same
  `metrics.k8s.io`-style false-negative lesson applied here deliberately
  from the start rather than rediscovered. Three more cover the capacity
  advisor's one new Role (`rbac/capacity-role.yaml`): list/get
  HorizontalPodAutoscalers in the lab namespace allowed, patching one or
  listing them outside the lab namespace denied. Five more cover the
  security auditor's two new Roles -- the cluster-scoped `ClusterRole`
  (`rbac/security-clusterrole.yaml`, checked without a namespace, since
  `ClusterRole`/`ClusterRoleBinding` are themselves cluster-scoped) and
  the namespaced `Role` (`rbac/security-role.yaml`, Roles/RoleBindings/
  PodDisruptionBudgets, lab namespace only).
- **`test_scheduling_live.py`** (4 tests, Milestone 3 of the platform spec)
  -- the `pending-resources`/`invalid-node-selector` scenarios' real Pods
  analyzed against the real cluster's actual Node allocatable/requested
  numbers (never mocked), confirming the engine's own independent
  constraint math isolates exactly which constraint blocked placement
  (`RESOURCE_FIT` for the former, `NODE_SELECTOR` alone -- with
  `RESOURCE_FIT` passing -- for the latter), that owner-reference
  resolution correctly names the owning Deployment, and the same
  HTTP-transport-level read-only check the optimization engine's own live
  tests already established.
- **`test_network_live.py`** (5 tests, Milestone 4 of the platform spec)
  -- the `port-mismatch-demo` scenario's real Pod analyzed against the
  real cluster's actual Service/EndpointSlice/container-port state (never
  mocked), confirming `CONFIGURATION_ERROR` from the named-targetPort
  mismatch alone and correct owning-Deployment resolution; the existing
  `service-selector-mismatch-demo` Service correctly leaves an unrelated,
  unselected Pod `INCONCLUSIVE`; real CoreDNS/`kube-dns` health read
  through the new `kube-system`-scoped RBAC grant; and the same
  HTTP-transport-level read-only check the scheduling/optimization
  engines' own live tests already established.
- **`test_capacity_live.py`** (3 tests, Milestone 5 of the platform spec)
  -- the `hpa-capacity-pressure-demo` scenario's real `HorizontalPodAutoscaler`
  (pinned `minReplicas=maxReplicas=1`, a deliberately tiny 1% CPU target)
  analyzed against the real cluster's actual HPA status and Node
  allocatable/requested numbers (never mocked), confirming `HPA_AT_MAX`
  fires from the real `ScalingLimited` condition and real over-target
  utilization, and that the live `HpaSnapshot` (replica bounds, metrics)
  is correctly attached to its `WorkloadSnapshot`; plus the same
  HTTP-transport-level read-only check every other engine's own live
  tests already establish. `SCALING_WOULD_BE_UNSCHEDULABLE` is
  deliberately *not* asserted here -- it is honestly environment-dependent
  on this lab's other scenarios' own current CPU usage (see
  `docs/CAPACITY_PLANNING.md` §11), confirmed live via the CLI/HTTP
  surfaces instead (see TODO.md's Milestone 23 entry) rather than pinned
  into an automated test that could flake as the lab's own state changes.
- **`test_security_live.py`** (6 tests, Milestone 6 of the platform spec)
  -- the `security-findings-demo` scenario's real privileged Pod, real
  over-permissive `ClusterRoleBinding`, and real unrestricted-ingress
  `NetworkPolicy` (never mocked) correctly produce
  `PRIVILEGED_CONTAINER`/`CLUSTER_ADMIN_BINDING`/`UNRESTRICTED_INGRESS_POLICY`
  findings; the cluster's own two real built-in RBAC bootstrap bindings
  (`system:masters`, `kubeadm:cluster-admins`) are confirmed live to never
  be flagged; a second `audit()` call is confirmed to replace, not
  accumulate alongside, the first run's findings (the live version of the
  real bug this milestone caught and fixed, see
  `docs/SECURITY_AUDITING.md`); plus the same HTTP-transport-level
  read-only check every other engine's own live tests already establish.
- **`test_mount_rule_live.py`** (2 tests, Milestone 1 audit closure) --
  the `failed-mount-demo` scenario's real Pod and real `FailedMount` Event
  (never hand-built) correctly diagnose as `FAILED_MOUNT` at HIGH
  confidence through the real `DiagnosticEngine`, and the real Event is
  confirmed to be a `has_failure_signal` on its own. This exact live pass,
  run through the real `ObserverService` end to end (a throwaway API
  server against the lab namespace, not just this file's own hand-built-
  evidence checks), is what caught the real `_RELEVANT_EVENT_REASONS` bug
  described above -- the real Pod-level incident did not open at all until
  that second filter list was fixed.
- **`test_optimization_inventory_live.py`** / **`test_optimization_live_scenarios.py`**
  (5 tests, Milestones 14/18) -- `build_workload_inventory()` against the
  real lab namespace matches known healthy/unhealthy workloads; the
  `missing-requests`/`oversized-resources` scenarios produce the expected
  `DATA_QUALITY`/no-proposed-number results against real cluster state, the
  latter additionally checked against a real (not mocked) near-zero
  metrics-server sample; and the read-only guarantee, extended from
  inventory-only to the *entire* `OptimizationService.analyze()` call
  (inventory + metrics-server query + persistence), by patching the HTTP
  transport itself and asserting only `GET` was ever issued.
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

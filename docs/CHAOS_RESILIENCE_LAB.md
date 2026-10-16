# Chaos & resilience evaluation harness

Milestone 7 of `docs/PLATFORM_ROADMAP.md`. Scope, verbatim from that doc:

> not new failure scenarios (16 already exist and are live-verified -- see
> FAILURE_LAB.md) but the **evaluation harness** named as missing in
> `IMPLEMENTATION_STATUS.md`: an automated way to apply a scenario, measure
> time-to-detection/time-to-diagnosis/recovery-time/false-positive rate
> against it, and persist that measurement for later comparison... Covers
> every analysis engine that exists by the time this milestone starts (A,
> B, and whichever of C/D/E/F are already built), not just the original
> troubleshooting scenarios.

Every number this harness reports is measured against a real kind cluster,
never estimated -- this document's own "explicitly bounded" instruction
from the roadmap, extended with the specifics below.

## 1. Why this is a new package, not an extension

`MASTER_ARCHITECTURE.md`'s "one pattern" describes an **analysis engine**:
its own package, reading the cluster, producing evidence-backed findings.
This capability does not fit that shape -- it doesn't find anything new
about the cluster; it *drives* and *times* the six engines that already
exist. It is this project's first genuinely different capability shape,
the same way capacity planning (extend, don't duplicate) and network's
bounded-probe decision (defer part of the spec on a safety basis) were
each a reasoned, explicitly-named exception rather than a silent deviation.
Stated plainly: `evaluation/` is a **test harness over the product**, not
a seventh product capability a user queries for cluster insight.

## 2. Ground truth: reusing the manifests' own comments, not a new DSL

Every one of this lab's 16 scenarios already states its own expected
diagnosis as a comment in its manifest (confirmed by reading all 16 before
writing this doc) -- e.g. `scenario-oom/deployment.yaml`: "Expected
KubeSentinel diagnosis: OOM_KILLED (confidence HIGH)...". The harness's
scenario registry (`evaluation/scenarios.py`) is a hardcoded Python list of
`ScenarioSpec` objects transcribing exactly those same, already-reviewed
expectations -- deliberately not a new YAML schema embedded in the
manifests themselves (that would mean two sources of truth, one machine-
read and one human-read, drifting apart over time) and not a parser that
extracts the comment text at runtime (fragile, and the comment prose is
written for a human, not a grammar). The registry is the only place that
ever needs updating when a new scenario is added.

Each `ScenarioSpec` carries: `name`, `lab_dir` (the `scenario-*` directory),
`resource_name_prefix` (how to find this scenario's own object among
everything else live in the shared lab namespace -- a Pod/Deployment name
prefix, or an exact name for the one PVC-only and one Service-only
scenario), `capability` (which of the six engines owns this scenario), and
`expected` (a capability-specific ground-truth value -- a `ReasonCode` for
troubleshooting, a `RecommendationCategory` for optimization/capacity, a
`NetworkFindingClassification` for network, an RBAC/container rule_id for
security, or `any_node_schedulable is False` for scheduling).

## 3. Push vs. pull: two genuinely different latencies, not one

Capability A (troubleshooting) is **push-based**: `ObserverService`'s
watchers run continuously, and `IncidentManager.observe()` diagnoses
inline, synchronously, the instant a watcher sees a relevant Pod/Event.
This means, honestly: **for every incidents-pipeline scenario,
time-to-detection and time-to-diagnosis are the same instant** in this
architecture -- there is no separate "noticed it, hasn't figured out why
yet" phase to measure. The harness computes both numbers anyway (from
`Incident.first_observed_at` and `Incident.diagnosis_history[0].performed_at`
respectively, both relative to the harness's own recorded apply time) and
reports them as what they are, rather than inventing an artificial gap
between them to make the harness output look more interesting than the
system it's measuring.

Capabilities B/C/D/E/F (optimization, scheduling, network, capacity,
security) are **pull-based**: a finding only exists after someone calls
`analyze()`/`audit()`. There is no latency internal to KubeSentinel to
measure here at all -- the real, interesting number is "how long after the
scenario is applied does the real Kubernetes/metrics-server state settle
enough that a *triggered* analysis pass correctly finds it," which is
dominated by cluster/kubelet/metrics-server timing, not KubeSentinel code.
The harness's own "time to detection" for these capabilities is: apply the
scenario, poll (re-triggering `analyze()`/`audit()` on each attempt) until
the expected finding appears or the timeout elapses. `oversized-resources`
is the one scenario that needs an unusually long timeout for this reason
(explained in its own manifest: at least 3 metrics samples spanning at
least 5 minutes before a proposed number can even exist) -- the harness's
default per-scenario timeout is 60s; this scenario's registry entry
overrides it to 400s. This is a real, structural wait, not a harness
inefficiency.

## 4. Recovery time: measured only where a scenario defines one

Recovery time is `Incident.recovery_duration_seconds`, read directly off
the already-resolved incident -- no new computation. Of the 16 scenarios,
exactly one (`scenario-transient-recovery`) is designed to self-heal; every
other scenario is deliberately left broken for inspection (that's the
point of a failure-lab scenario) and has no fix path at all, automatic or
otherwise. Forcing a "recovery time" number out of a scenario with no
recovery path would mean either fabricating one or silently cleaning up
and calling that "recovery," neither of which this project does elsewhere.
`ScenarioResult.recovery_time_seconds` is `None` for every scenario except
`transient-recovery`, which is the honest, "honest about absence" answer
established as this project's existing discipline, not a gap the harness
should try to paper over.

## 5. False-positive rate: a whole-battery property, not a per-scenario one

A false positive needs a *negative* case to be false against, and no
individual failure scenario provides one (it is, by construction,
genuinely broken -- any finding against it is by definition a true
positive if it matches, or a diagnostic-accuracy question if it
doesn't, never a false positive). The shared lab namespace, however,
already gives this harness a real negative case for free: everything in
`kubesentinel-lab` that isn't one of the 16 registered scenarios' own
resources (and every Node-/`kube-system`-scoped object this project reads)
is supposed to be healthy. After a full battery run, the harness lists
every currently-open incident/finding across all five repositories and
classifies each one: attributable to a registered scenario's own
`resource_name_prefix` (expected), or not (a false positive). The reported
`false_positive_rate` is `false_positive_count / total_open_findings_count`
at the end of the run -- a real, reproducible count against the real
cluster's actual current state, not a synthetic injection.

## 6. How the harness drives the cluster and the engines

**Apply/cleanup**: `subprocess.run(["kubectl", "apply", "-f", lab_dir])` /
`kubectl delete ... -l kubesentinel.io/scenario=<name>` -- reusing exactly
what `lab/scripts/run-scenario.sh`/`cleanup.sh` already do, under whatever
kubeconfig context is ambient (the operator's own, same as running those
scripts by hand). This is lab tooling, not a product code path: it never
goes through `ClusterClients`, needs no new RBAC grant, and is a
deliberate, explicit exception to "read-mostly against the cluster" for
exactly the same reason `RemediationExecutor` already is one -- except
this mutation is scoped to the lab namespace's own scenario-labeled
objects, is development/CI tooling rather than a production code path at
all, and is never exposed over the HTTP API (CLI-only; see §8).

**Every run starts from a clean slate.** The lab namespace is shared,
long-lived state (some scenarios have been live for this entire project's
history) -- if the harness measured time-to-detection against a scenario
that's already been sitting there for two days, with an incident that
already opened days ago, every number would read as "0s," which would be
true and also useless. `evaluate` always deletes-if-present, confirms the
delete (polls for the object's absence, bounded), *then* records the apply
timestamp and applies -- so every measurement starts from a genuine,
verified absence, regardless of what pre-existing lab state happens to be
sitting around. Running the full battery is therefore disruptive to the
shared lab's current state by design (every scenario gets torn down and
recreated); this is expected of an evaluation harness and is why it is a
deliberate, explicit, developer-invoked command, never run automatically
or on a schedule.

**Measuring against Capability A** (push-based): the harness builds its
*own* short-lived `ObserverService` (same constructor, same watchers,
`ClusterClients`/`Settings` built exactly like `cli/main.py`'s existing
helpers), starts it, polls its own `IncidentRepository` for the scenario's
expected incident, then stops the watcher threads once matched or timed
out. This duplicates no watch logic -- it reuses `ObserverService`
wholesale -- and does not require a separately-running API server process;
a real deployment's own long-running server, if also watching the same
cluster concurrently, would observe the same objects independently and
redundantly but not incorrectly (`IncidentManager.observe()` is already
idempotent per resource UID).

**Measuring against Capabilities B/C/D/E/F** (pull-based): the harness
calls the relevant service directly, in-process, exactly the way
`cli/main.py`'s `optimize`/`security_audit` commands already do --
`OptimizationService.analyze()`, `SecurityService.audit()` for whole-
namespace passes; `SchedulingService.analyze_pod()` /
`NetworkService.analyze_pod()` for the two per-Pod engines, resolving the
Pod name via the same `label_selector=f"app={...}"` lookup
`test_mount_rule_live.py` already established as this project's own
pattern for "find today's randomly-suffixed Pod name for this scenario."
Each poll attempt re-calls `analyze()`/`audit()` -- never reads stale,
previously-persisted findings as if they were fresh.

## 7. Persistence and models

`models/evaluation.py`: `EvaluationCapability` (enum: `TROUBLESHOOTING`,
`OPTIMIZATION`, `SCHEDULING`, `NETWORK`, `SECURITY` -- capacity planning
is `OPTIMIZATION` with `category=CAPACITY`, the same non-duplication this
project already applies to capacity everywhere else), `DetectionOutcome`
(enum: `DETECTED`, `WRONG_CATEGORY`, `TIMED_OUT`), `ScenarioResult`
(per-scenario outcome + the three timing fields + what was actually
observed), `EvaluationRun` (a `run_id`, start/end timestamps, the list of
`ScenarioResult`s, and the whole-battery false-positive count/rate from
§5). A sixth sibling repository, `EvaluationRepository` behind
`persistence/interface.py` (`save_run`/`get_run`/`list_runs`, mirroring
every existing repository's shape) -- the same one SQLite file, never a
second database.

## 8. Surface: CLI-only for driving, HTTP for reading

Running a battery mutates the shared lab namespace (§6) and can take
several minutes (the `oversized-resources` timeout alone). This is
deliberately **not** exposed as a `POST` HTTP endpoint the way `/optimize`
or `/security/audit` are -- unlike those, which are read-only against the
cluster and safe to trigger from anywhere the API token reaches, this one
is lab-mutating developer/CI tooling, the same boundary that already keeps
`lab/scripts/*.sh` CLI-only and out of the HTTP surface entirely.
`kubesentinel evaluate [--scenario NAME ...] [--all] [--json]` is the only
way to *run* a battery. `GET /evaluation/runs` and
`GET /evaluation/runs/{run_id}` (read-only, following every other
capability's existing GET-serves-persisted-data convention) let anything
that already polls the HTTP API for incidents/findings/recommendations
read a past evaluation run the same way, and the matching
`evaluation-runs`/`evaluation-run <id>` CLI commands exist for symmetry
with every other capability's own read commands.

## 9. What this milestone does not attempt

- No scheduled/periodic execution of the battery (the roadmap's own scope
  line: "an automated way to apply a scenario and measure... and persist
  that measurement for later comparison" -- a human or CI invokes it and
  compares runs over time; it does not invoke itself).
- No new lab scenarios (16 already exist, per the roadmap's own scope).
- No attempt to make `time_to_detection` and `time_to_diagnosis` read as
  two different numbers for Capability A when the architecture genuinely
  only has one phase there (§3) -- reporting that honestly is this
  milestone's job, not hiding it.
- No injected/synthetic false-positive scenario -- §5's whole-battery
  definition uses the real, already-live lab state instead.

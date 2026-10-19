# Evidence completeness: a shared vocabulary across capabilities

## 1. The question this closes

`docs/MASTER_ARCHITECTURE.md`'s own "Observability shape" section left
this explicitly open: "there is no shared three-state 'healthy /
degraded / unknown-because-unavailable' model across capabilities
today. The optimization engine expresses this ad hoc
(`metrics_available: bool` + a `warning: str | None`). Before a third
and fourth capability each invent their own slightly-different version
of the same idea, it's worth deciding whether this becomes a shared,
typed enum... or stays per-capability by deliberate choice. Not
decided by this document -- flagged for the milestone that first needs
it."

That milestone is this one. All six capabilities now exist, and --
exactly as the note predicted -- each independently invented its own
version before this was decided:

| Capability | Model | How it expresses "something's missing" |
|---|---|---|
| B (optimization) | `AnalysisRunSummary` | `metrics_available: bool` + `warning: str \| None` (singular) |
| F (security) | `SecurityAuditResult` | `warnings: list[str]` (a list, no completeness verdict alongside it) |
| C (scheduling) | `SchedulingFinding` | nothing -- a sub-read failure was logged, never surfaced |
| D (network) | `NetworkFinding` | nothing, and structurally couldn't be without first changing `analyze_pod`'s own error-handling granularity -- closed as a later, separate follow-up (§7) |
| A (troubleshooting) | `Hypothesis` | `confidence` (HIGH/MEDIUM/LOW/INCONCLUSIVE) -- a different question (how sure is this diagnosis), not covered by this decision |
| E (capacity) | reuses B's `AnalysisRunSummary` | same as B |

## 2. The decision

A shared, typed enum, included via a common base model -- the exact
shape `MASTER_ARCHITECTURE.md`'s own parenthetical already suggested
("a common base the per-capability summary models each include").

```python
# models/completeness.py
class EvidenceCompleteness(str, Enum):
    COMPLETE = "COMPLETE"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"

class EvidenceCompletenessMixin(BaseModel):
    warnings: list[str] = Field(default_factory=list)
    evidence_completeness: EvidenceCompleteness = EvidenceCompleteness.COMPLETE
```

Four states, matching the note's own named three ("degraded / unknown /
not applicable") plus the implicit positive case it was contrasted
against:

- **`COMPLETE`**: every read this pass attempted succeeded; nothing in
  `warnings`.
- **`DEGRADED`**: the pass still produced a usable result, but at
  least one sub-read failed or fell back -- `warnings` says which.
- **`UNKNOWN`**: no meaningful result could be produced at all (the
  relevant backend was entirely unreachable, not just one sub-read
  among several). Not exercised by any of the four retrofits (§4/§7) --
  each of them always produces *some* usable result even when degraded
  (even `NetworkFinding`'s own DNS-check-failure case still produces a
  full, meaningful finding for everything Service/port/policy-related),
  so `UNKNOWN` never actually applies to the whole run for any of them.
  Named here as a real state this vocabulary supports, not one these
  retrofits happened to need.
- **`NOT_APPLICABLE`**: this check's own precondition doesn't hold for
  the target at all. Also not exercised -- a run-level result (an
  entire `analyze()`/`audit()` pass, or one Pod's own finding) is never
  itself "not applicable," only a specific per-target check within it
  could be (e.g. a CPU-throttling check on a container with no CPU
  limit set). `UsageSummary.throttled`'s own existing `bool | None`
  tri-state already covers exactly that narrower case well;
  deliberately not retrofitted onto this enum here (§6).

Named explicitly: two of the four states exist in the shared
vocabulary for future, narrower, per-target uses -- not because these
retrofits happened to produce them.

## 3. Why additive, never a rename

Every existing per-capability field is kept exactly as it was.
`AnalysisRunSummary.warning`/`.metrics_available` still mean exactly
what they always meant; `SecurityAuditResult.warnings` already had the
identical name and shape the mixin uses, so adopting it needed no
rename at all, only a dropped duplicate declaration. This gives every
retrofitted model one more, shared, cross-capability-comparable way to
say the same thing, never a breaking change to a persisted field or an
existing caller. Confirmed: the full pre-existing unit test suite
passed unchanged before any new test was added for this closure.

## 4. The first three models retrofitted

Each already had an analogous ad hoc mechanism to retrofit onto:

- **`AnalysisRunSummary`** (`optimization/service.py::analyze()`):
  `evidence_completeness = DEGRADED if not metrics_available else
  COMPLETE`, `warnings = [warning] if warning else []` -- computed
  from the exact same information the pre-existing fields already
  carry, never a second, independently-derived answer to the same
  question.
- **`SecurityAuditResult`** (`security/engine.py::SecurityAuditEngine.audit()`):
  `evidence_completeness = DEGRADED if inventory.warnings else
  COMPLETE`. `warnings` itself needed no new computation -- it already
  was this exact field.
- **`SchedulingFinding`** (`scheduling/service.py::analyze_hypothetical`,
  `scheduling/analyzer.py::analyze_pod_scheduling`): the one case with
  a genuine behavior change, not just a new field. `analyze_hypothetical`'s
  own ResourceQuota/LimitRange reads already caught `ApiException` and
  degraded to an empty list on failure -- but only logged it
  (`logger.info`), never told the caller. A finding could silently
  report "no ResourceQuota in this namespace" (`satisfied=True`) when
  the true state was "couldn't check" -- the exact
  absence-of-evidence-as-evidence-of-absence this project's own
  discipline elsewhere never allows (the same class of gap
  `check_namespace_quota`'s/`check_limit_range`'s own precedent was
  built to avoid, just missed on this one path). Now each failure is
  appended to a `warnings: list[str]` collected in `service.py` and
  threaded through `analyze_pod_scheduling`'s own new `warnings`
  parameter, which sets `evidence_completeness` on the returned
  finding. The real Pod analysis path (`analyze_pod`) never checks
  quota/LimitRange at all (by design -- an already-admitted Pod already
  passed both), so it always passes no warnings and stays `COMPLETE`.

## 5. `NetworkFinding`, closed as a later, separate follow-up

`network/service.py::analyze_pod` was genuinely all-or-nothing:
`check_dns_health`/`build_service_match_results`/`build_applicable_policies`
were called directly, with no individual `try`/`except` around any of
them -- any failure aborted the entire analysis (caught by one outer
`except Exception`, surfaced as a 503), unlike optimization's/security's/
scheduling's own inventory builders, which each wrap individual
sub-reads and degrade one at a time.

### 5.1 Which sub-reads are genuinely degradable, and which stay fatal

Not every sub-read is an equally good degrade candidate -- the same
judgment `scheduling/service.py` already makes for its own
ResourceQuota/LimitRange reads (admission-time-only, genuinely
optional) versus its core Node/Pod reads (foundational, correctly
fatal):

- **Stay fatal**: the Pod read itself, and
  `build_service_match_results` (the core Service/Pod/EndpointSlice
  evidence this finding is actually *about*). There is no meaningful
  finding left to produce without either.
- **Now degradable**: ReplicaSet listing (only used for owner
  resolution, feeding `related_incident_ids`) and
  `build_applicable_policies` (NetworkPolicy evaluation) -- each wrapped
  in its own `try`/`except ApiException`, degrading to `None`/`[]`
  respectively and appending a warning, rather than aborting the whole
  analysis.

### 5.2 A second, more subtle instance of the same gap, found in the process

`check_dns_health` already caught `ApiException` on each of its own
three sub-reads (the coredns Deployment, the kube-dns Service, its
EndpointSlices) and degraded to `DnsHealthResult(healthy=False, ...)`
-- it never propagated a failure up to `analyze_pod` at all. But
`healthy=False` alone conflates two different claims: "DNS is
confirmed down" and "I couldn't tell, the read itself failed." A 403
reading the coredns Deployment would have produced a confident-looking
`DNS_UNHEALTHY` finding, indistinguishable from a genuine outage.

Fixed by adding `DnsHealthResult.check_succeeded: bool = True`, set to
`False` on each of the three failure branches (never touching
`healthy`, so the pre-existing classification behavior and its own
test suite are unchanged). `analyze_pod` now checks this flag and
appends `dns_health.reasoning` to the finding's own `warnings` whenever
it is `False` -- the classification still says `DNS_UNHEALTHY` (this
function has no way to second-guess a health result it didn't compute
itself), but the finding's `warnings`/`evidence_completeness` now tell
a caller the difference.

### 5.3 What's still fatal, and why that's correct

`PodNotFoundError` and any failure inside
`build_service_match_results` still abort the whole analysis (a 503,
unchanged). This is not an oversight left over from before this
closure -- it is the same "is this sub-read foundational or genuinely
optional" judgment applied correctly: a finding with no idea which
Services even exist, or whether the target Pod exists at all, has
nothing left to report.

## 6. What remains deliberately untouched, named explicitly

- **`Hypothesis.confidence`** (troubleshooting engine). A genuinely
  different question -- "how sure is this diagnosis," not "did every
  sub-read succeed" -- already well-served by its own four-level scale
  (HIGH/MEDIUM/LOW/INCONCLUSIVE). Forcing it onto this enum would
  conflate two distinct concepts for no real gain.
- **`UsageSummary.throttled: bool | None`** (optimization engine). A
  narrower, already-well-designed tri-state for one specific
  per-container question (confirmed-throttled / confirmed-not /
  no-signal-available). Could theoretically be re-expressed using
  `EvidenceCompleteness` (`NOT_APPLICABLE` when no CPU limit is set at
  all, `UNKNOWN` when Prometheus is unreachable, otherwise a real
  boolean) -- not done here, since the existing scheme already works
  correctly and retrofitting it would touch a persisted, already-tested
  field for no behavioral gain, purely cosmetic consistency. A real
  candidate for a later pass if this vocabulary ever needs a second,
  narrower-than-run-level example.

With `NetworkFinding` now included, all four models this project's
docs ever pointed to as having "invented their own version" of this
idea (§1's own table) share the one vocabulary. The two items above
are the only remaining named exceptions, and both are deliberate, not
oversights.

## 7. Live-verified against the real cluster, all four retrofitted models

Ran `kubesentinel optimize`, `kubesentinel security-audit --json`, a
real `POST /scheduling/simulate` call, and `kubesentinel network-check`
against a real throwaway Pod -- all against the actual
`kind-kubesentinel` cluster. Confirmed:

- Optimization: `evidence_completeness: DEGRADED`, `warnings:
  ["metrics.k8s.io pods query failed (status=404): Not Found"]` --
  matching the pre-existing `warning` field exactly (this lab has no
  metrics-server installed, the same honest-absence state documented
  throughout this project's history).
- Security: `evidence_completeness: COMPLETE`, `warnings: []` -- every
  RBAC-gated read this engine makes succeeded on this real cluster.
- Scheduling: `evidence_completeness: COMPLETE`, `warnings: []` for a
  real simulation with no sub-read failures.
- Network: `evidence_completeness: COMPLETE`, `warnings: []`,
  `dns_health.check_succeeded: true` for a real Pod with no sub-read
  failures.

Every failure path across all four models (a ResourceQuota/LimitRange
read denied by RBAC, a ReplicaSet/NetworkPolicy listing denied, a DNS
sub-read failing) is covered by unit tests with a real
`ApiException(403)`, not reproduced by revoking this project's own
real RBAC mid-session -- deliberately avoided as an unnecessarily
risky action on a shared cluster for paths already provably correct
under test.

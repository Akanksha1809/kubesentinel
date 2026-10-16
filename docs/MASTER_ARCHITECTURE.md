# Master architecture

The platform-wide view: what's built, what's planned, and the one
architectural pattern every analysis engine in this project follows,
proven six times now (troubleshooting, then resource optimization, then
the scheduling advisor, then the network troubleshooting engine, then
capacity planning, then the security & configuration auditor) -- the
full platform spec's own Capability A-F list, now complete.

This document does not re-explain any already-built pipeline in detail --
`ARCHITECTURE.md` covers the troubleshooting and resource-optimization
pipelines in depth, including their own "why this shape" reasoning and
what's deliberately not built in each; `docs/SCHEDULING_ANALYSIS.md`
covers the scheduling advisor the same way. This document is the layer
above all of them: how every capability should slot into the same shape,
and why that shape, not a fresh one per capability, is the right call for
this project specifically.

See `docs/IMPLEMENTATION_STATUS.md` for the detailed, evidence-backed
status of every capability named below, and `docs/PLATFORM_ROADMAP.md` for
the milestone order.

## The one pattern, stated once

Every analysis engine in this project, built or planned, is:

1. **Its own package** under `src/kubesentinel/<capability>/`, with its own
   `models/<capability>.py` (or fields added to a shared model only where
   the two domains genuinely share structure -- `IMPLEMENTATION_STATUS.md`
   names where that line is) -- **unless the platform spec itself
   explicitly asks for extension over duplication**, as it did for
   capacity planning (a `capacity.py` submodule inside `optimization/`,
   fields added to `models/optimization.py`, not a new package/model file
   at all). That exception is itself the rule working correctly, not a
   violation of it: reuse over duplication is point 3 below, and the spec
   overriding point 1 in one specific, named case is a decision this
   project followed rather than overrode on its own judgment.
2. **Read-mostly against the cluster.** Every engine lists/gets Kubernetes
   objects; only the troubleshooting engine's `RemediationExecutor` is
   currently permitted to mutate anything, and that permission is narrow,
   RBAC-scoped, and goes through `PolicyEngine` first. A new capability
   gets zero mutation permission by default -- it has to be deliberately
   added, the same way `remediation-role.yaml` was added as a *separate*
   Role from the read-only `role.yaml`, never folded into it.
3. **Sharing infrastructure, never duplicating it**: the one
   `ClusterClients` bundle (extended additively when a capability needs an
   API group nothing currently constructs -- see "Client surface" below,
   never a second client-construction path), the one SQLite file (a new
   sibling repository class behind `persistence/interface.py`, never a
   second database), the one FastAPI app (a new `APIRouter`, never a
   second app process), the one CLI (`cli/main.py`, new `@app.command()`
   functions), and the one Prometheus registry
   (`metrics/registry.py`, new metrics with the same bounded-cardinality
   label discipline every existing metric already follows).
4. **Deterministic first.** Every finding a capability produces is
   evidence-backed and reproducible from the same input. An optional local
   AI layer may explain an already-computed finding in plain English; it
   never computes the finding itself and never has a path back into any
   decision -- the exact shape `ai/summarizer.py` already proves out for
   the troubleshooting engine.
5. **Honest about absence.** A capability whose data source is unavailable
   (no metrics-server, no Prometheus, kindnet not enforcing
   NetworkPolicy, no HPA installed) says so explicitly -- `metrics_available:
   false` plus a `warning` string is this project's established shape for
   that, not a silent fallback to zero or "healthy."

This is not an aspirational list -- it is what `optimization/`,
`scheduling/`, `network/`, and `security/` all already do, verified
against `diagnostics/`'s own original shape. Six data points is still
not a law of nature, but the pattern has now held up through live
verification six times without needing to bend, including three times
with a genuine reason to extend it: a read-only placement *simulation*
(`scheduling/`, deserializing a hypothetical, not-yet-real Pod spec into
a typed object and analyzing it exactly like a real one); a deliberate,
documented scope boundary (`network/`, where the spec's own active
connectivity probe was reasoned out of this milestone entirely rather
than built -- this project's first analysis engine to defer part of its
own spec on a safety basis, not pick-and-choose expediency); and a
deliberate *non*-package (capacity planning, where the spec's own
"extend, don't duplicate the metrics collector" instruction was followed
literally -- zero new persistence, zero new API routes, zero new CLI
commands, because the structures it extends were already generic enough
to carry a new enum value and an optional field with no further code).
The security auditor (`security/`) is a fourth data point for the *base*
pattern holding without needing to bend at all -- its own genuine
decision was build-vs-integrate an external scanner (§2 of
`docs/SECURITY_AUDITING.md`), not a deviation from this shape. All three
extensions were written down explicitly in their own design docs
(`docs/SCHEDULING_ANALYSIS.md`, `docs/NETWORK_DIAGNOSTICS.md`,
`docs/CAPACITY_PLANNING.md`) rather than silently absorbed into "the
pattern" retroactively -- which is exactly the discipline this paragraph
asks every future capability to follow too.

## Client surface: what exists, what each future capability needs

`observer/client.py::ClusterClients` today bundles `core` (`CoreV1Api`),
`apps` (`AppsV1Api`), `discovery` (`DiscoveryV1Api`), `custom_objects`
(`CustomObjectsApi`, for `metrics.k8s.io`), `batch` (`BatchV1Api`),
`networking` (`NetworkingV1Api`), `autoscaling` (`AutoscalingV2Api`),
`rbac_authorization` (`RbacAuthorizationV1Api`), and `policy`
(`PolicyV1Api`) -- each added additively, in the milestone that first
needed it, never ahead of time (`custom_objects`/`batch` were added
specifically for the optimization engine; `networking` for the network
troubleshooting engine; `autoscaling` for capacity planning;
`rbac_authorization`/`policy` for the security auditor; nothing before
each needed them). This table is now complete -- every capability the
platform spec named (A-F) has shipped:

| Capability | New client needed | Status |
|---|---|---|
| C: Scheduling | none -- `core.list_node()` already existed on the existing `core` client | **done** -- `scheduling/node_inventory.py` calls it; confirms the prediction that no new client was needed here |
| D: Network | `NetworkingV1Api` (NetworkPolicy; no Ingress -- confirmed live that zero Ingress/Gateway resources or CRDs exist on this cluster) | **done** -- `network/policy_inventory.py` calls it |
| E: Capacity | `AutoscalingV2Api` (HPA) | **done** -- `optimization/capacity.py` calls it |
| F: Security | `RbacAuthorizationV1Api` (Roles/Bindings), `PolicyV1Api` (PodDisruptionBudget) | **done** -- `security/inventory.py` calls both |

Each addition follows the existing pattern exactly: extend the
`ClusterClients.__init__` signature, extend `build_clients()`, and add the
new RBAC grants to a new, separate `rbac/<capability>-role.yaml` (never
folded into an existing Role) with verbs scoped to exactly what that
capability's own code calls -- the same audit trail
`rbac/optimization-role.yaml` already established, including its own
documented reasoning for *not* granting a capability nothing uses yet
(see that file's own docstring).

## Data model boundary

`IMPLEMENTATION_STATUS.md` names the one real cross-capability
relationship that already exists: a resource-optimization recommendation
can carry `related_incident_ids`, a by-ID link (never a live object
reference) to the troubleshooting engine's own incidents. This is the
precedent for how future capabilities should relate to each other and to
the first two: a typed, nullable, by-ID link field on the *newer*
capability's own model, pointed at the *older* one's primary key, added
only when there's a concrete reason (here: "don't recommend a resize
alongside an unresolved failure on the same workload"). Never a shared,
all-capabilities "finding" supertype with a growing pile of optional
fields most capabilities leave empty -- the spec's own instruction ("use
separate types for different domains... do not create one enormous generic
model") matches what this project already does by construction, not by
restraint.

**Built**: a Scheduling finding referencing the Pending-pod incident it
explains -- `SchedulingService._open_incident_ids_for()`, the same dual-UID
(Pod's own UID, owning workload's UID), deduped, no-repo-wired-up-is-a-
safe-no-op lookup `OptimizationService` already does. Caught live during
this milestone's own verification, worth naming as a reminder: the field
was declared on `SchedulingFinding` from the design doc onward but not
actually wired up until a live check against the real cluster showed it
staying empty -- the exact kind of scaffolded-but-unpopulated gap this
project's own Milestone 0 audit flagged for `UsageSummary.throttled` in
the *other* engine. Caught here before it shipped the same way, not after.

Plausible future links, not yet built, named here so they're a deliberate
decision later rather than an accidental omission now:

- A Security finding referencing the workload it's about (no incident
  link needed -- security findings aren't about failures).
- A Capacity recommendation referencing the resource-optimization
  recommendation(s) it's built from (extends the existing engine, per the
  spec's own instruction, rather than re-deriving usage history).

## Policy boundary: what's proven, what isn't yet

`PolicyEngine` (`policy/engine.py`) is the only mutation gate this project
has ever built, and it was built for, and only ever tested against, the
troubleshooting engine's own five `ActionType`s
(RESTART_DEPLOYMENT/ROLLBACK_DEPLOYMENT/SCALE_DEPLOYMENT plus the two
advisory `RECOMMEND_*` types). The resource optimization engine's human
review step deliberately does not route through it (see
`IMPLEMENTATION_STATUS.md`'s "Shared policy layer" section for the full
reasoning) because a review is a decision record, not a cluster mutation.

This matters for every future capability that might eventually want to
*act* (apply a right-sized resource spec, patch a NetworkPolicy, scale a
Deployment for capacity reasons, tighten a security setting): **none of
that inherits policy enforcement automatically.** `PolicyEngine.evaluate()`
would need a real new `ActionType` per action, a real new executor method
(mirroring `RemediationExecutor`'s own "only typed objects, never
`.to_dict()` in a patch body" discipline -- see TODO.md #11 for why that
distinction is a real, previously-caught footgun, not a style preference),
and an explicit call into `PolicyEngine.evaluate()` before anything
executes. The spec's own list of non-negotiables (no automatic resource
optimization, no automatic NetworkPolicy edits, no automatic
security-policy weakening, no automatic destructive action) is therefore
satisfied today by simple absence -- none of that code exists yet -- not by
a policy check actively blocking it. The day any of those apply paths gets
built, it must be built *through* `PolicyEngine`, not around it, and this
document is where that expectation is written down before the temptation
to take a shortcut exists.

## Observability shape

Every metric this project exports uses the same bounded-cardinality
discipline (`category`, `action_type`, `reason`, `component`, `status`,
`provider` as label values -- never an ID). A future capability's metrics
should follow the exact naming convention already established:
`kubesentinel_<capability>_<noun>_total`/`_active`/`_duration_seconds`,
matching `kubesentinel_optimization_*`'s own naming exactly.

One real gap, named in `IMPLEMENTATION_STATUS.md` and repeated here because
it's an architectural decision, not an implementation detail: there is no
shared three-state "healthy / degraded / unknown-because-unavailable"
model across capabilities today. The optimization engine expresses this
ad hoc (`metrics_available: bool` + a `warning: str | None`). Before a
third and fourth capability each invent their own slightly-different
version of the same idea, it's worth deciding whether this becomes a
shared, typed enum (e.g. on a common base the per-capability summary
models each include) or stays per-capability by deliberate choice. Not
decided by this document -- flagged for the milestone that first needs it.

## What this document does not cover

Troubleshooting and resource-optimization pipeline internals: see
`ARCHITECTURE.md`. RBAC specifics and the threat model: see
`RBAC_AND_THREAT_MODEL.md`. Milestone sequencing and acceptance criteria:
see `PLATFORM_ROADMAP.md`. Exact capability-by-capability status with file
citations: see `IMPLEMENTATION_STATUS.md`.

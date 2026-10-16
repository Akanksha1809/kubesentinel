# Kubernetes operator

Milestone 8 of `docs/PLATFORM_ROADMAP.md`. Scope, per that doc: "one or
more CRDs (`KubeSentinelPolicy` and/or `KubeSentinelRemediationPolicy`...
scoped down if their value isn't concrete enough to justify both) plus a
thin reconciliation controller... the controller calls into the existing
`PolicyEngine`/service-layer interfaces, never reimplements their logic a
second time... Defaults to observation and recommendation; any actual
remediation triggered through the operator still goes through the same
approval mechanism everything else does."

## 1. One CRD, not two

`KubeSentinelPolicy` has no concrete content to carry that
`KubeSentinelRemediationPolicy` doesn't already cover: the only policy
*gate* this project has ever built is `PolicyEngine` (see
`MASTER_ARCHITECTURE.md`'s "Policy boundary" section -- resource
optimization's review step and every other capability's findings are
explicitly *not* policy-gated at all, by design). A second, more generic
CRD would either duplicate the first or carry fields nothing reads.
Scoped down to one: **`KubeSentinelRemediationPolicy`**, namespaced,
group `kubesentinel.io`, version `v1alpha1`.

## 2. What the controller does NOT do

This is the load-bearing constraint, worth stating before anything else:
**the controller never decides which `ActionType` to propose for an
incident.** Reading `remediation/service.py::RemediationService.propose()`
confirms that decision has never existed anywhere in this codebase as
reusable logic -- a human (via the CLI's `plan` command or the API's
`POST /incidents/{id}/actions/plan`) always supplies the `action_type`
explicitly today. Every rule through the diagnostics engine sets
`hypothesis.auto_remediation_permitted=False` on every hypothesis it
produces (`policy/engine.py`'s own comment: "today, correctly, nothing is
ever auto-execution-eligible"). Building a controller that invents this
mapping (diagnosis -> which action to take) would be writing a real,
new, nontrivial decision system inside "a thin reconciliation
controller" -- exactly the overreach `MASTER_ARCHITECTURE.md`'s policy
boundary section warns against for any *new* apply path, and this
project's own conservative discipline (never fabricate a decision it
hasn't earned the evidence for) rules out doing it here first.

What the controller *does* do instead is narrower and fully supported by
code that already exists and is already tested: it re-evaluates
*already-proposed, still-pending* `RemediationAction`s against the
policy a `KubeSentinelRemediationPolicy` CR declares, and auto-approves
and executes the ones that now qualify -- the same `PolicyEngine.evaluate()`
decision and the same `RemediationService.approve()`/`execute()` calls a
human would otherwise make by hand, just performed automatically when
policy says they're allowed. "The same approval mechanism everything
else does" is satisfied literally: the controller calls `approve()`, it
does not skip it.

## 3. Why this is still valuable, concretely

Today, `PolicyEngine`'s mode (`OBSERVE`/`APPROVAL_REQUIRED`/
`CONTROLLED_AUTO`) and thresholds (cooldown, per-incident/per-window
budgets) are process-wide `Settings`, set once from environment
variables at process start. There is no way to change policy without
restarting the server, and no way to scope policy differently per
namespace at all. A `KubeSentinelRemediationPolicy` CR makes this
declarative and namespace-scoped: `kubectl apply` a CR with
`mode: CONTROLLED_AUTO` for the `kubesentinel-lab` namespace, and any
action a human already proposed there under the old `APPROVAL_REQUIRED`
mode and left `PENDING_APPROVAL` gets picked up and auto-approved on the
next reconcile, without touching the running server's environment or
restarting it. Change the CR back to `APPROVAL_REQUIRED`, and the next
reconcile simply stops finding anything newly eligible -- it never
revokes an already-executed action, since execution is one-way.

## 4. CRD schema

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: kubesentinelremediationpolicies.kubesentinel.io
spec:
  group: kubesentinel.io
  scope: Namespaced
  names:
    kind: KubeSentinelRemediationPolicy
    plural: kubesentinelremediationpolicies
    singular: kubesentinelremediationpolicy
    shortNames: ["ksrp"]
  versions:
    - name: v1alpha1
      served: true
      storage: true
      subresources:
        status: {}
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                mode: {type: string, enum: [OBSERVE, APPROVAL_REQUIRED, CONTROLLED_AUTO]}
                cooldownSeconds: {type: integer, minimum: 0}
                maxActionsPerIncident: {type: integer, minimum: 1}
                maxActionsPerWindow: {type: integer, minimum: 1}
                windowSeconds: {type: integer, minimum: 1}
                allowedResourceNames: {type: array, items: {type: string}}
              required: [mode]
            status:
              type: object
              properties:
                observedGeneration: {type: integer}
                lastReconcileTime: {type: string}
                pendingActionsSeen: {type: integer}
                actionsAutoApproved: {type: integer}
                message: {type: string}
```

Every `spec` field mirrors an existing `Settings` field 1:1
(`policy_mode`, `action_cooldown_seconds`, `max_actions_per_incident`,
`max_actions_per_window`, `action_window_seconds`,
`action_allowed_resource_names`) -- no new policy concept, just a
namespace-scoped, declarative way to set values that were previously
only global and environment-variable-configured. There is no
`allowedNamespaces` field: the CR's own `.metadata.namespace` *is* the
namespace it governs, one CR per namespace a human has deliberately
opted in. A namespace with no CR gets no reconciliation at all --
explicit opt-in, matching "defaults to observation."

## 5. Reconciliation, precisely

For a given `KubeSentinelRemediationPolicy` CR (on create/update, and on
a bounded resync interval):

1. Build a scoped `Settings` copy: `get_settings().model_copy(update={...` from the CR's own `spec` fields `})` -- every other field (`db_path`, `kubeconfig`, etc.) stays whatever the operator process's own environment already set. This is **not** an attempt to mutate a separately-running API server's in-memory config across a process boundary (impossible, and not attempted) -- the operator is its own process, reading the same SQLite file and the same cluster, exactly like a CLI invocation does.
2. Build `PolicyEngine(scoped_settings)` and `RemediationService(policy_engine, executor, verifier, incident_repo, scoped_settings)` -- the *exact* classes and constructor shape `cli/main.py`'s existing command wiring already uses, never a parallel implementation.
3. `incident_repo.list(namespace=cr.metadata.namespace)`, filter to incidents with at least one `RemediationAction` in `PENDING_APPROVAL`.
4. For each such action: re-read the target Deployment's current `spec.replicas` live (never trust a stale value -- the same "read live, don't assume" discipline this project applies everywhere), build a fresh `PolicyRequest` from the action's and incident's own already-recorded fields (`action_type`, `target`, `incident_id`, `incident.severity`, the hypothesis that diagnosed it), and call `policy_engine.evaluate()`.
5. If the outcome is now `ALLOWED`: call `remediation_service.approve(incident, action.action_id, approved_by="kubesentinel-operator")` then `.execute(incident, action.action_id)` -- the identical two calls a human makes via the CLI/API, just invoked by the operator and auditable as such (`approved_by` names the operator, never impersonates a person).
6. Patch the CR's `.status` (`observedGeneration`, `lastReconcileTime`, `pendingActionsSeen`, `actionsAutoApproved`) -- read-only bookkeeping on the operator's own CR, not a remediation action, so it is never policy-gated (the same reasoning `IncidentRepository.save()` already isn't).

Nothing here recomputes a diagnosis, invents an action type, or
second-guesses a human's `plan` call -- it only re-asks the question
`propose()` already asks, against policy that may have changed since.

## 6. No new dependency: reusing `observer/watchers.py`, not `kopf`

`IMPLEMENTATION_STATUS.md` named `kopf` as a plausible dependency for
this milestone when it was still unbuilt. Decided against it: this
project already has a proven, tested, synchronous list-then-watch
abstraction (`observer/watchers.py::_ListWatchLoop`) driving six resource
types, including one (`EndpointSliceWatcher`) that already handles a
resource whose generated client returns a *dict*, not a typed model --
the exact shape a CRD watch needs, since `CustomObjectsApi` has no typed
model for any CRD at all (confirmed already in this codebase:
`optimization/metrics_providers.py`'s `MetricsApiProvider` already reads
`response.get("items", [])` off `list_namespaced_custom_object`, a plain
dict). Adding `kopf` would mean a second, parallel reconciliation
paradigm (asyncio-based) bridged into this project's synchronous,
threading-based one, for exactly one watcher -- a real integration cost
for a framework whose only job here is the same list/watch/backoff loop
this project has already built and tested five times over. A seventh,
thin `CustomResourceWatcher` class alongside `PodWatcher`/`PVCWatcher`/
etc. is simpler, dependency-free, and consistent.

`ClusterClients.custom_objects` (`CustomObjectsApi`) already exists --
added for the resource optimization engine's `metrics.k8s.io` provider.
No new client needed.

## 7. Where it runs

A new CLI subcommand, `kubesentinel operator run`, blocking (like
`uvicorn` blocks for the API server) -- not a second executable or
entrypoint, keeping "the one CLI" convention `MASTER_ARCHITECTURE.md`
already states. In a real cluster deployment this runs as its own
container/Pod with its own Deployment manifest (`lab/operator-deployment.yaml`),
using the same `kubesentinel` ServiceAccount every other component uses
(this is a local demo project with one logical identity, not a
multi-tenant system that would warrant a dedicated one).

## 8. RBAC

A new, narrow `rbac/operator-role.yaml` (namespaced, `kubesentinel-lab`),
additive to the existing Roles bound to the same `kubesentinel`
ServiceAccount -- never folded into `role.yaml` (read-only) or
`remediation-role.yaml` (Deployment mutation), matching this project's
own "separately auditable blast radius" precedent:

```yaml
rules:
  - apiGroups: ["kubesentinel.io"]
    resources: ["kubesentinelremediationpolicies"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["kubesentinel.io"]
    resources: ["kubesentinelremediationpolicies/status"]
    verbs: ["get", "patch", "update"]
```

The actual remediation mutation (restart/rollback/scale a Deployment)
reuses the *existing* `remediation-role.yaml` grant unchanged -- the
operator performs the exact same cluster-mutating calls
`RemediationExecutor` already makes for a human-approved action, so it
needs exactly the same permissions, not a new or broader set.

## 9. What this milestone does not attempt

- No auto-proposal of remediation actions (§2).
- No cross-process config push to an already-running API server --
  the operator is its own, independent reader of the same cluster and
  database (§5 step 1).
- No second CRD (§1).
- No new third-party operator framework (§6).
- No change to `PolicyEngine`'s own decision code at all -- every check
  it performs today is exercised unchanged; the operator is a new
  *caller*, never a new implementation.

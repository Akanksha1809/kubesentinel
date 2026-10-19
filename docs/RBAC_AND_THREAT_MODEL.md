# RBAC and threat model

## Principle

RBAC grants grow alongside implemented capability, not ahead of it. No
`cluster-admin` anywhere. The observer's Role (below) stays read-only; the first
write verbs in the project (Milestone 4) live in a **separate** Role
(`rbac/remediation-role.yaml`) so "what can read the cluster" and "what can
change it" stay independently auditable in two files, not interleaved in one.

```yaml
# rbac/role.yaml (current, in full) -- namespace-scoped
rules:
  - apiGroups: [""]
    resources: ["pods", "events", "persistentvolumeclaims", "services"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["discovery.k8s.io"]
    resources: ["endpointslices"]
    verbs: ["get", "list", "watch"]
```

```yaml
# rbac/clusterrole.yaml (current, in full) -- the one cluster-scoped exception
rules:
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list", "watch"]
```

Node is not a namespaced resource, so watching it (Milestone 2's `NodeConditionRule`)
cannot be expressed in a namespaced Role no matter how narrow -- this is the one
necessary ClusterRole in the whole project. `KUBESENTINEL_WATCH_NODES=false` disables
the Node watcher entirely for a deployment that can't grant cluster-scoped RBAC; the
ClusterRole/ClusterRoleBinding aren't needed in that case.

```yaml
# rbac/remediation-role.yaml (current, in full) -- the first write permissions
# in the project, deliberately a separate Role from the observer's above
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "patch", "update"]
  - apiGroups: ["apps"]
    resources: ["deployments/scale"]
    verbs: ["get", "patch"]
  - apiGroups: ["apps"]
    resources: ["replicasets"]
    verbs: ["get", "list"]
```

Matches exactly what `RemediationExecutor` does: `patch` on `deployments` covers
restart (an annotation patch); `update` covers rollback, which issues a full HTTP
PUT (`replace_namespaced_deployment`) -- **not** covered by `patch` alone. This
was a real gap caught by reading the generated client's own request method
rather than assuming: every live verification of rollback in this project ran
under an admin kubeconfig, not the `kubesentinel-remediator` ServiceAccount, so
it would not have surfaced a 403 if this Role had shipped with only `patch`.
`deployments/scale` covers the scale subresource used for `SCALE_DEPLOYMENT`;
read-only `replicasets` is needed only to read revision history for rollback,
never patched or deleted. No `delete` verb anywhere in this Role. Never on
Nodes/RBAC/CRDs/admission resources, matching the spec's explicit prohibition on
KubeSentinel modifying cluster-wide resources automatically.

```yaml
# rbac/optimization-role.yaml (current, in full) -- a third, separate Role,
# for the resource optimization engine (Milestone 17)
rules:
  - apiGroups: ["apps"]
    resources: ["statefulsets", "daemonsets", "replicasets"]
    verbs: ["get", "list"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list"]
  - apiGroups: ["metrics.k8s.io"]
    resources: ["pods"]
    verbs: ["get", "list"]
```

Same independent-auditability rationale as splitting observer from
remediator: this Role covers exactly what `optimization/inventory.py` and
`metrics_providers.py` actually call. Verbs are `get`/`list` only, never
`watch` -- the optimization engine takes a one-shot inventory snapshot per
`analyze()` call, unlike the troubleshooting engine's continuous Pod watch,
so there is no watch call anywhere in this module to grant for.
`replicasets` is declared here too even though `remediation-role.yaml`
already grants the same ServiceAccount read access to it, so this Role
alone stays sufficient if remediation were ever removed.

**A separate `metrics.k8s.io` Nodes ClusterRole was deliberately withheld
when this Role was first written, then added in a later, separate
milestone once a real caller existed** -- `MetricsApiProvider
.get_current_node_usage()` had existed since Milestone 13 with no caller
anywhere in this codebase, and granting RBAC ahead of the code that uses
it is exactly what this project's RBAC principle (above) exists to avoid.
`OptimizationService.analyze()` gained that caller (the cluster-level
overcommitment analysis's real-usage ratio, RESOURCE_OPTIMIZATION_DESIGN.md
§15.5), so `rbac/optimization-clusterrole.yaml`/
`-clusterrolebinding.yaml` now grant exactly that, `metrics.k8s.io/nodes`
`get`/`list` only -- the fourth documented cluster-scope exception in this
project, after the Node ClusterRole (Capability A), the network engine's
`kube-system`-bound RoleBinding (Capability D), and the security auditor's
ClusterRole (Capability F). `rbac/optimization-role.yaml` above is
unchanged by this -- `metrics.k8s.io/nodes` is cluster-scoped and
genuinely cannot be granted by a namespaced Role, no matter how narrow.

```yaml
# rbac/scheduling-role.yaml (current, in full) -- a fourth, separate Role,
# for the scheduling & placement advisor (Milestone 3 of the platform spec)
#
# limitranges grown onto this same Role later, for check_limit_range
# (docs/SCHEDULING_ANALYSIS.md §12) -- LimitRange is namespaced, same
# as ResourceQuota above, so this stays one Role, not a new one.
rules:
  - apiGroups: [""]
    resources: ["resourcequotas", "limitranges"]
    verbs: ["get", "list"]
```

The smallest Role this project has added yet, for a reason worth stating:
Nodes are already granted by `clusterrole.yaml` (Capability A's
`NodeConditionRule`), Pods/Deployments by `role.yaml`, ReplicaSets by
`remediation-role.yaml` (reused here for the same owner-reference
resolution `optimization/ownership.py` already does) -- the scheduling
advisor's own package needs *zero* new grants for any of that. The two
genuinely new permissions are `resourcequotas`/`limitranges`, used only
for the namespace-quota/LimitRange checks in a *hypothetical*-spec
simulation (`POST /scheduling/simulate`) -- never checked for a real,
already-existing Pod, since a real Pod already passed admission and
neither can explain why it's Pending. `get`/`list` only, same
"one-shot snapshot, never a watch" reason as `optimization-role.yaml`.

```yaml
# rbac/network-role.yaml (current, in full) -- a fifth, separate Role,
# for the network troubleshooting engine (Milestone 4 of the platform spec)
rules:
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get", "list"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["networkpolicies"]
    verbs: ["get", "list"]
```

```yaml
# rbac/network-dns-role.yaml + rbac/network-dns-rolebinding.yaml
# (current, in full) -- a deliberate, narrow exception to this project's
# "lab namespace only" boundary, bound in kube-system itself (the one
# other RoleBinding namespace in the whole project, alongside
# clusterrole.yaml's own cluster-scoped exception for Nodes). A
# RoleBinding can only reference a Role in its *own* namespace, so this
# Role and its binding both live in kube-system -- the subject (the
# kubesentinel ServiceAccount) still lives in kubesentinel-lab, proving a
# RoleBinding's own namespace and its subject's namespace are independent.
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get"]
    resourceNames: ["coredns"]
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get"]
    resourceNames: ["kube-dns"]
  - apiGroups: ["discovery.k8s.io"]
    resources: ["endpointslices"]
    verbs: ["list"]
```

`services`/`networkpolicies` cover exactly what `network/service_inventory.py`
and `policy_inventory.py` call, scoped to the lab namespace like every
other Role -- Pods/Deployments/ReplicaSets are already granted by
`role.yaml`/`remediation-role.yaml` and reused as-is for selector matching
and owner-reference resolution. The `kube-system` exception is scoped as
narrowly as RBAC's `resourceNames` allows: `get` on exactly the
`coredns` Deployment and the `kube-dns` Service by name (not "any
Deployment/Service in kube-system"), plus `list` on EndpointSlices (not
name-scopable, since they're queried by label selector, not by name).
This is the same category of deliberate, documented exception as
`clusterrole.yaml`'s Node grant -- a capability that genuinely needs
something outside the lab namespace, granted as narrowly as the API
allows and called out explicitly, never folded silently into a wider Role.

```yaml
# rbac/capacity-role.yaml (current, in full) -- a sixth, separate Role,
# for the capacity planning & autoscaling advisor (Milestone 5 of the
# platform spec)
rules:
  - apiGroups: ["autoscaling"]
    resources: ["horizontalpodautoscalers"]
    verbs: ["get", "list"]
```

The smallest Role this project has added since `scheduling-role.yaml`,
for the identical reason: Nodes (reused via
`scheduling/node_inventory.py` for this engine's own scale-headroom
check) are already granted by `clusterrole.yaml`; Pods/Deployments/
StatefulSets/DaemonSets/Jobs are already granted by `role.yaml`/
`optimization-role.yaml`. The only genuinely new permission is
`horizontalpodautoscalers`, read-only, scoped to the lab namespace like
every other Role.

This entire boundary -- all six Roles plus the kube-system exception,
both directions (what's granted and what isn't) -- is now covered by
`tests/integration/test_rbac_boundary.py`: 33 automated checks,
authenticated as the real `kubesentinel` ServiceAccount's own minted token
plus `kubectl auth can-i --as=...`, run against the live cluster every
time `./scripts/test.sh --all` does. This is the repeatable version of
what Milestone 4's one-off manual verification caught; it isn't a one-time
check anymore. One exception, found live while verifying the optimizer
Role (Milestone 18): `kubectl auth can-i` itself gave a *wrong* answer
(`no`) for `metrics.k8s.io` pods even after the Role genuinely granted it
-- `can-i`'s client-side API discovery doesn't always know about
aggregated APIs. `test_can_get_pod_metrics_via_real_authenticated_call`
checks this one via a real authenticated request instead of `can-i`,
specifically because the convenience tool was confirmed unreliable for it;
see KUBERNETES_CONCEPTS.md. A related but distinct lesson surfaced
verifying the network engine's `resourceNames`-scoped `kube-system` grant:
plain `can-i get deployments.apps -n kube-system` (no resource name)
correctly reports `no` even though the Role *does* grant `get` on
`coredns` specifically -- not a bug, but a reminder that a
`resourceNames`-scoped rule must be checked with `kubectl`'s own
`TYPE/NAME` syntax (`deployments.apps/coredns`), which `kubectl_can_i()`
now supports via an optional `resource_name` parameter. The capacity
advisor's own `horizontalpodautoscalers.autoscaling` checks needed no
such workaround -- confirmed live that plain `can-i` answers correctly
for this ordinary, non-aggregated API group.

```yaml
# rbac/security-clusterrole.yaml (current, in full) -- a new ClusterRole,
# for the security & configuration auditor's RBAC checks (Milestone 6 of
# the platform spec). THE THIRD documented cluster-scope exception in
# this project, after the Node ClusterRole (Capability A) and the network
# engine's kube-system-bound RoleBinding (Capability D) -- ClusterRole/
# ClusterRoleBinding are themselves cluster-scoped resources, so no
# namespaced Role can grant read access to them, no matter how narrow.
#
# `namespaces` (core API group) grown onto this same ClusterRole later,
# for NamespaceWithoutPodSecurityStandardRule (docs/SECURITY_AUDITING.md
# §15) -- Namespace is also cluster-scoped, same reasoning as the rule
# above, so this stays the one cluster-scope exception for this
# capability rather than becoming a second ClusterRole for the same
# ServiceAccount.
rules:
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["clusterroles", "clusterrolebindings"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["get", "list"]
```

```yaml
# rbac/security-role.yaml (current, in full) -- the namespaced half:
# Roles/RoleBindings genuinely are namespace-scoped, so this grant
# belongs in a namespaced Role; PodDisruptionBudgets too, lab namespace
# only. Pods/Deployments/StatefulSets (container-security + PDB-coverage
# checks) are already granted by role.yaml/optimization-role.yaml;
# NetworkPolicy read access is already granted by network-role.yaml --
# all reused, none re-granted.
rules:
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["roles", "rolebindings"]
    verbs: ["get", "list"]
  - apiGroups: ["policy"]
    resources: ["poddisruptionbudgets"]
    verbs: ["get", "list"]
```

```yaml
# rbac/optimization-clusterrole.yaml (current, in full) -- a new
# ClusterRole, closing the optimization engine's own previously-named
# gap: MetricsApiProvider.get_current_node_usage() had existed since
# this engine's own early milestones but had no caller and no grant for
# this cluster-scoped resource. THE FOURTH documented cluster-scope
# exception in this project, after the Node ClusterRole (Capability A),
# the network engine's kube-system-bound RoleBinding (Capability D),
# and the security auditor's ClusterRole (Capability F) --
# `metrics.k8s.io/nodes` is Node-level, not namespaced, so no namespaced
# Role (rbac/optimization-role.yaml, which already grants the
# namespaced `metrics.k8s.io/pods`) can grant it.
rules:
  - apiGroups: ["metrics.k8s.io"]
    resources: ["nodes"]
    verbs: ["get", "list"]
```

This entire boundary -- every Role/ClusterRole in this project, plus
the kube-system exception, both directions -- is now covered by
`tests/integration/test_rbac_boundary.py`: 44 automated checks (42
passing in an environment without metrics-server installed, like this
project's own lab; 2 honestly skip rather than fail, since
"metrics-server isn't reachable" is not an RBAC question either way).
The security auditor's own RBAC rules needed a real, found-live
correction of their own, worth recording here rather than only in
TODO.md: the built-in-subject exclusion (`CLUSTER_ADMIN_BINDING`) first
assumed only the well-known `system:masters` Group needed excluding,
but this project's own real kind cluster has a *second* built-in
cluster-admin Group binding -- `kubeadm:cluster-admins`, bound to the
Group kubeadm itself uses for `admin.conf` -- confirmed by reading this
cluster's own real `ClusterRoleBindings` (`kubectl get
clusterrolebindings`) rather than trusting the one better-known
identity. Fixed by excluding Group subjects by *prefix* (`system:`,
`kubeadm:`) rather than one hardcoded exact name;
`test_builtin_rbac_bootstrap_bindings_are_never_flagged` in
`tests/integration/test_security_live.py` pins both exclusions against
this project's own real cluster.

A second real, found-live correction, caught while adding the Node-metrics
ClusterRole above: `test_can_get_pod_metrics_via_real_authenticated_call`
(the Pod-metrics sibling of this section's own
`test_can_get_node_metrics_via_real_authenticated_call`) only treated a
`503` as "metrics-server unavailable, not an RBAC question" -- but this
exact lab's own permanent state (no metrics-server installed at all, not
installed-but-unreachable) makes the real apiserver return a plain `404`
for the unregistered aggregated API path instead, which the test's own
`except` block re-raised as a hard failure rather than skipping. Running
it live (confirming `get_current_node_usage()`'s own RBAC grant) surfaced
that this sibling test had been silently broken in this project's own
lab the whole time it existed. Fixed to treat `404` the same as `503` --
both mean "the aggregated API itself can't answer right now," neither is
a `403` and neither says anything about this ServiceAccount's own grant.

## What will be added, and when

- **ConfigMaps/Secrets** (if `MISSING_CONFIG_REFERENCE` ever needs to confirm a
  reference's existence beyond what the kubelet's own waiting-reason message already
  gives for free -- not needed as of Milestone 2, since `CreateContainerConfigError`'s
  message already names the missing object) -- read-only, existence check only. See
  below for why Secret *values* are a hard line, not a TODO.

## Secrets are never read for their values

Nothing in this codebase requests `get`/`list` on `secrets`, and nothing will be
added for it: `MissingConfigReferenceRule` (Milestone 2) gets everything it needs
from the kubelet's own `CreateContainerConfigError` waiting message, which already
names the missing ConfigMap/Secret/key -- it never needs to read a Secret's
contents, only to see that the kubelet failed to resolve it. If a future rule ever
needs to check a Secret's mere *existence* (not its values), that needs its own
explicit justification in this document, not a silent addition to an existing Role.

## Trust boundaries

- **Pod/Event/log/annotation content is untrusted input.** A workload can put
  arbitrary text in its container's log output, event messages, or its own
  annotations/labels. `inspector.py`'s evidence extraction stores this as `data`
  values on `Evidence` objects and never executes, templates, or interprets it as
  anything but a string to display -- there is currently no code path that would feed
  this text into a shell, a query, or a decision about authorization.
- **Diagnostic rules never execute anything.** `DiagnosticRule.evaluate()` is a pure
  function; there is no code path from evidence content to a subprocess, shell, or
  dynamic code execution.
- **The policy engine (`policy/engine.py::PolicyEngine`) is the sole authority on
  remediation.** Every mutating action goes through `evaluate()` first --
  mode (OBSERVE/APPROVAL_REQUIRED/CONTROLLED_AUTO), namespace/resource-name
  allowlists, cooldown, and per-incident/per-window budgets -- before
  `RemediationExecutor` ever sees it. `diagnostics/` only ever produces
  hypotheses and non-binding recommendations; it has no path to the cluster at
  all. See REMEDIATION_SAFETY.md for what's actually enforced and the live
  verification evidence (including the Milestone 4 RBAC gap this policy
  separation would not by itself have caught -- the executor's own narrow Role
  is what did).
- **The resource optimization engine (`optimization/`) is read-only by
  construction, not by convention.** `inventory.py`/`analyzer.py`/`safety.py`
  never call a Kubernetes client method other than `list_*`/`get_*` -- a
  live integration test patches the HTTP transport itself and asserts only
  `GET` methods were ever issued during a full `analyze()` pass (inventory,
  metrics-server call, and persistence together, not just inventory
  collection). There is no `ActionType`, executor method, or write RBAC verb
  anywhere in this engine's own Role that could patch a workload's
  resources -- a future apply path (not built) would need to be added
  explicitly, through the *existing* `PolicyEngine`/`RemediationExecutor`
  lifecycle, not a parallel one.
- **The scheduling advisor (`scheduling/`) is read-only by construction,
  the same way.** `node_inventory.py`/`constraints.py`/`analyzer.py` never
  call a write method either -- a live integration test confirms only
  `GET` during a full `analyze_pod()` pass, the same check the
  optimization engine already has. Even the *simulation* endpoint (`POST
  /scheduling/simulate`) never creates the hypothetical Pod it analyzes --
  the raw spec is deserialized into a typed object and evaluated in
  memory, nothing is ever sent to the API server as a creation request.
- **AI (`ai/summarizer.py`) has no Kubernetes credentials, no database access, and
  no path to the policy/remediation engines at all.** `summarize_incident()` takes
  an already-built `Incident` object and an `AiClient`; it never touches
  `ClusterClients`, `SQLiteIncidentRepository`, or `PolicyEngine`, and nothing in
  `api/app.py`'s `/ai-summary` endpoint or the CLI's `explain` command lets the
  model's output flow back into a decision -- `AiSummary` has no field a caller
  could mistake for an action or an approval. `build_prompt()` only includes the
  same structured `Hypothesis`/`RemediationAction` fields a human already sees via
  `kubesentinel evidence`, never raw Pod/Event/annotation text -- so there is
  nothing in the prompt a workload could use to try to inject instructions into
  the model. The only backend implemented is a local Ollama server: no API key,
  no outbound network call, no cloud account, genuinely free rather than "free
  tier." Disabled by default (`KUBESENTINEL_AI_ENABLED=false`) -- every other
  feature in this project works fully without it, matching the spec's "first
  release works without any AI model" requirement literally, not just for the
  first release.

## Sensitive-data handling

No code currently persists Secret values or raw credentials. `Evidence.data` and
`Hypothesis` text are built only from Pod status/condition/container-state fields
(phase, reasons, messages, exit codes, restart counts) -- none of which are
credential material by nature of what Kubernetes puts in `PodStatus`. Redaction
logic for log excerpts (planned alongside richer log-based evidence) is a Milestone 2+
item, not yet needed since no rule reads container logs in Milestone 1.

## API authentication

Enforced on every mutating endpoint (`POST /incidents/{id}/actions/
plan|{action_id}/approve|{action_id}/execute|{action_id}/reject`) via
`api/app.py::require_api_token`, a FastAPI dependency checked before any of
them run. An **unset** `KUBESENTINEL_API_TOKEN` returns `503` (disabled),
never `200` -- there is no code path where "no token configured" is treated as
"no auth needed," matching the spec's "do not expose an unauthenticated,
unrestricted remediation endpoint" literally rather than only in spirit. A
configured-but-wrong `Authorization` header is `401`. Read-only endpoints
(`/incidents`, `/evidence`, `/metrics`, `/healthz`, `/readyz`) remain
unauthenticated -- they expose only KubeSentinel's own local state (nothing
read from the cluster that the underlying kubeconfig's own RBAC doesn't
already gate), and the spec's authentication concern is specifically about
unrestricted *write* access. Confirmed live with real `curl` requests (both the
`503` and `401` paths) in Milestone 6, and exercised again via the CLI's
`dry-run`/`plan`/`approve`/`execute`/`reject` commands, which construct the
same `RemediationService` directly (no HTTP, no token check -- the CLI is a
local, already-privileged client by nature, consistent with every other CLI
command in this project). See REMEDIATION_SAFETY.md.

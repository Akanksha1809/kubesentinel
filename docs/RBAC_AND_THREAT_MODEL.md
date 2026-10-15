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

This entire boundary -- both Roles, both directions (what's granted and what
isn't) -- is now covered by `tests/integration/test_rbac_boundary.py`
(Milestone 9): 16 automated checks, authenticated as the real `kubesentinel`
ServiceAccount's own minted token plus `kubectl auth can-i --as=...`, run
against the live cluster every time `./scripts/test.sh --all` does. This is
the repeatable version of what Milestone 4's one-off manual verification
caught; it isn't a one-time check anymore.

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

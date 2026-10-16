# Security and configuration auditor (Milestone 6 of the platform spec)

## 1. What already exists, reused here

A new `security/` package (the fourth genuinely new top-level package in
this project, after `scheduling/`/`network/` -- `optimization/capacity.py`
was deliberately *not* a new package, per Milestone 5's own design doc).
Reused as-is, by import, never re-derived:

- `diagnostics/engine.py::DEFAULT_RULES`'s own shape -- "a list of rule
  objects, each a pure function over already-fetched evidence, evaluated
  uniformly, empty result means nothing found" -- applied to a new
  domain. `security/rules/base.py::SecurityRule` mirrors
  `diagnostics/rules/base.py::DiagnosticRule` structurally.
- `optimization/ownership.py::resolve_pod_owner` -- reused for the
  container-security checks' own Pod -> owning-workload grouping, the
  same proven utility `scheduling/`/`network/` already reuse rather than
  re-deriving owner resolution a third/fourth time.
- `network/policy_inventory.py` -- the NetworkPolicy coverage checks
  reuse its existing ingress/egress rule-summary classification
  (`_summarize_policy`, made public the same way
  `scheduling/constraints.py::requirement_matches` was promoted for
  Milestone 4's own reuse) rather than re-reading and re-classifying
  NetworkPolicy objects from scratch -- the roadmap's own explicit
  instruction.
- `optimization/quantities.py` -- not reused (no resource-quantity
  parsing needed here); named only to confirm it was checked and genuinely
  doesn't apply, not silently skipped.
- The persistence/API/CLI *shape* -- a new sibling repository behind
  `persistence/interface.py`, a new `APIRouter`, new `@app.command()`
  functions -- proven four times now (scheduling, network; capacity
  deliberately skipped this shape by design). Findings here have no
  review lifecycle (`NEW -> REVIEWED -> APPROVED/REJECTED -> EXPIRED`,
  the optimization engine's own shape) -- a security/config finding isn't
  a proposal a human approves or rejects, it's a fact about the cluster's
  current state, read-only start to finish, matching
  `SchedulingFinding`/`NetworkFinding`'s own no-lifecycle shape instead.

## 2. Build vs. integrate: a native rule catalog, not kube-linter/Kubescape

The spec leaves this as a genuine decision for this milestone's own
design pass. Built natively, not integrated, for three concrete reasons:

1. **Consistency.** Every other analysis engine in this project
   (Capabilities A-E) is a native, deterministic, in-repo rule catalog.
   No external scanner binary, no subprocess execution surface, exists
   anywhere in this codebase today -- confirmed by grep. Integrating one
   here would be the first time this project shells out to a third-party
   tool for its *core* logic, a materially different category of
   dependency than the existing *optional data sources*
   (Prometheus/Ollama/metrics-server), which this engine's own findings
   never depend on to function at all.
2. **Scope fits.** The checks this milestone needs (container security
   context, RBAC over-permissiveness, NetworkPolicy coverage, PDB
   coverage) are each a handful of field reads against already-typed
   Kubernetes objects -- not the kind of deep, maintained-elsewhere
   expertise (CVE databases, compliance framework mappings) that
   justifies pulling in a whole external project.
3. **"Every optional component is genuinely optional" stays honest.** If
   kube-linter/Kubescape were integrated as "optional," the base auditor
   would need to duplicate most of their checks anyway for the
   non-optional path to be useful alone -- the same shape Ollama/
   Prometheus/metrics-server never need, since those three are purely
   additive data, never the only source of a capability's own logic.

## 3. Scope: what this milestone evaluates for real vs. defers

**Evaluated for real**, each a deterministic field read against a real,
typed Kubernetes object -- never a CVE scan, never a compliance-framework
score:

1. **Container security context** (`security/rules/container.py`),
   deduped per owning workload the same way `optimization/inventory.py`
   already does:
   - `PRIVILEGED_CONTAINER` -- `securityContext.privileged == true`.
   - `ALLOW_PRIVILEGE_ESCALATION` -- explicitly `true`, or *not
     explicitly set to `false`* (reported at lower severity -- an
     unset field is a real gap worth naming, not silently assumed safe,
     the same "absence is evidence" discipline
     `optimization/analyzer.py::_data_quality_recommendation` already
     established for missing requests/limits).
   - `RUN_AS_ROOT` -- effective `runAsNonRoot` (container-level falling
     back to Pod-level) is not `true`.
   - `MISSING_READ_ONLY_ROOT_FS` -- `readOnlyRootFilesystem` is not `true`.
   - `HOST_NAMESPACE_SHARING` -- any of `hostNetwork`/`hostPID`/`hostIPC`
     is `true` on the Pod spec.
   - `HOST_PATH_VOLUME` -- any `hostPath` volume on the Pod spec.
   - `MUTABLE_IMAGE_TAG` -- an image reference with tag `:latest` or no
     tag at all (the exact gotcha already documented live in
     TROUBLESHOOTING.md for an unrelated reason -- reused here as a
     security/config finding too, not re-discovered).
   - `SECRET_AS_ENV_VAR` -- a container sources a Secret via
     `env[].valueFrom.secretKeyRef` or `envFrom[].secretRef` -- flagged
     because env vars are more exposure-prone (process listing, crash
     dumps, logs) than a volume-mounted Secret, never because reading a
     Secret's own value (never done anywhere in this project -- see
     RBAC_AND_THREAT_MODEL.md).
2. **RBAC over-permissiveness** (`security/rules/rbac.py`), reading real
   `ClusterRoleBinding`/`RoleBinding`/`ClusterRole`/`Role` objects:
   - `CLUSTER_ADMIN_BINDING` -- any binding referencing the built-in
     `cluster-admin` ClusterRole, **except** the cluster's own one
     built-in binding whose subject is exactly the `system:masters`
     Group (matched by subject, not by binding name, since a binding
     could be renamed) -- every other cluster-admin grant (a
     ServiceAccount, a User, or any other Group) is a real, actionable
     finding.
   - `WILDCARD_RBAC_RULE` -- any `Role`/`ClusterRole` rule with `*` in
     `verbs` *and* (`*` in `resources` or `*` in `apiGroups`), excluding
     Kubernetes' own built-in Roles (name starts with `system:`, or is
     exactly `cluster-admin`/`admin`/`edit`/`view`) -- these are
     intentionally broad by Kubernetes' own design and not something a
     cluster operator can "fix," so flagging them would be pure noise
     with no actionable remediation.
3. **NetworkPolicy coverage** (`security/rules/network_policy.py`,
   reusing `network/policy_inventory.py`):
   - `NAMESPACE_WITHOUT_NETWORK_POLICY` -- a namespace with running Pods
     has zero NetworkPolicy objects at all.
   - `UNRESTRICTED_INGRESS_POLICY` -- a NetworkPolicy's `policyTypes`
     includes `Ingress` but at least one ingress rule has no `from`/
     `ports` restriction at all (an effective allow-all), reusing
     `network/policy_inventory.py`'s own rule-presence classification.
   - Both carry the exact same kindnet-non-enforcement caveat Milestone 4
     already established verbatim: a configuration gap finding, never a
     claim about actually-observed traffic.
4. **PodDisruptionBudget coverage** (`security/rules/availability.py`),
   the roadmap's named optional item, included since it's a simple,
   valuable, standard check:
   - `MISSING_PDB_FOR_MULTI_REPLICA_WORKLOAD` -- a Deployment/StatefulSet
     with `desired_replicas >= 2` has no PodDisruptionBudget whose
     selector matches it.

**Explicitly deferred, stated once here rather than per-finding:**

- **Resource requests/limits auditing.** Already fully covered by
  Capability B's own `DATA_QUALITY` category (`MISSING_CPU_REQUEST` etc.)
  -- duplicating it here would be exactly the "re-reading a second time"
  the roadmap's own NetworkPolicy instruction already warns against,
  generalized to every other already-covered domain too.
- **CVE/image vulnerability scanning.** Needs a maintained vulnerability
  database this project has no access to and no intention of building;
  this is the single clearest example of where an external tool
  (Trivy/Grype) would genuinely add capability this native catalog
  cannot responsibly claim, named explicitly as a gap rather than
  silently absent.
- **Pod Security Admission namespace-label auditing** (the modern
  replacement for the deprecated PodSecurityPolicy). A real, valuable
  check, but deliberately out of this milestone's first pass -- the
  container-level checks above already cover the same underlying
  concerns (privilege, host namespaces, root) more granularly and
  per-workload; a namespace-label check would be a coarser, redundant
  view of the same evidence. Named as a candidate for a later pass, not
  forgotten.
- **ServiceAccount token automount auditing**
  (`automountServiceAccountToken`). Named as a real, specific gap this
  milestone does not cover -- a reasonable next addition to
  `security/rules/rbac.py`, not built now because it's a materially
  different signal (token *exposure*, not permission *scope*) from the
  RBAC-over-permissiveness checks already built, and this milestone's
  own scope is already substantial.
- **Compliance framework scoring** (CIS Benchmarks, PCI-DSS, etc.). No
  such mapping exists or is claimed; every finding here stands on its
  own evidence, never a framework section number.

## 4. Module layout

```
security/
  __init__.py
  inventory.py      build_security_inventory() -- one-shot read of every
                     object the rules below need (Pods grouped by owning
                     workload via resolve_pod_owner, ClusterRoleBindings/
                     RoleBindings/ClusterRoles/Roles, NetworkPolicies +
                     Pod presence per namespace, Deployments/StatefulSets
                     + PodDisruptionBudgets). No watch loop -- the same
                     one-shot-snapshot shape every other engine uses.
  rules/
    base.py          SecurityRule ABC, mirrors diagnostics/rules/base.py
    container.py      the 7 container-level rules (§3.1)
    rbac.py            the 2 RBAC rules (§3.2)
    network_policy.py the 2 NetworkPolicy rules (§3.3)
    availability.py    the 1 PDB rule (§3.4)
  engine.py          SecurityAuditEngine -- DEFAULT_RULES list, runs every
                     rule against the inventory, mirrors
                     diagnostics/engine.py::DiagnosticEngine exactly
  service.py         SecurityService -- the one module with side effects,
                     mirrors NetworkService/SchedulingService
```

## 5. Data model

```python
class SecuritySeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

class SecurityResourceRef(BaseModel):
    # namespace=None means cluster-scoped (ClusterRole/ClusterRoleBinding) --
    # never "" (the empty string), which would misrepresent a cluster-scoped
    # object as being "in the empty namespace."
    namespace: str | None = None
    kind: str
    name: str

class SecurityFinding(BaseModel):
    finding_id: str
    rule_id: str                    # e.g. "PRIVILEGED_CONTAINER"
    severity: SecuritySeverity
    resource: SecurityResourceRef
    # The owning workload, when the resource is a Pod/container-level
    # finding -- None for RBAC/NetworkPolicy/PDB findings, which have no
    # single owning workload.
    workload_kind: str | None = None
    workload_name: str | None = None
    evidence: list[str]             # plain-text facts this finding is based on
    explanation: str
    remediation: str
    created_at: datetime

class SecurityAuditResult(BaseModel):
    # One per audit() call -- mirrors AnalysisRunSummary's own
    # freshness-marker shape, persisted as the latest run only.
    audit_id: str
    analyzed_at: datetime
    namespaces: list[str]
    findings: list[SecurityFinding]
    rules_evaluated: list[str]
    # A rule that could not be evaluated at all (e.g. 403 listing
    # ClusterRoleBindings) is named here, distinct from "evaluated and
    # found nothing" -- the same AnalysisRunSummary.warning discipline,
    # generalized to per-rule granularity since this engine runs several
    # independent rule families in one pass, not one metrics query.
    warnings: list[str] = Field(default_factory=list)
```

`SecurityFinding` rows are individually persisted and addressable
(`get_finding`/`list_findings`, filterable by namespace/severity/
`rule_id`) -- the roadmap's own "finding ID, rule ID, severity..." wording
names these as first-class entities, the same shape
`SchedulingFinding`/`NetworkFinding` already established, not a single
opaque blob per run.

## 6. API and CLI

- `POST /security/audit` -- the only route that touches the cluster;
  body `{"namespaces": [...]}`, empty means every watched namespace.
- `GET /security/findings[?namespace=&severity=&rule_id=]` -- `severity`
  a typed Enum query param from the start (Milestone 18's lesson, applied
  here without rediscovering it).
- `GET /security/findings/{finding_id}`.
- CLI: `security-audit`, `security-findings`, `security-finding`.

## 7. Safety

Entirely read-only. No code path in `security/` ever creates, patches, or
deletes anything -- not a Role, not a Pod, not a NetworkPolicy. This is
true of every capability in this project except the fully policy-gated
`RemediationExecutor`, and stays true here.

## 8. RBAC

A new `ClusterRole` (not a namespaced `Role`) -- the **third** documented
cluster-scope exception in this project, after the Node `ClusterRole`
(Capability A) and the network engine's `kube-system`-bound `RoleBinding`
(Capability D). `ClusterRoleBinding`/`ClusterRole` are themselves
cluster-scoped resources; no namespaced `Role` can grant read access to
them, no matter how narrow.

```yaml
# rbac/security-clusterrole.yaml -- a new, separate ClusterRole,
# for the security & configuration auditor (Milestone 6 of the
# platform spec)
rules:
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["clusterroles", "clusterrolebindings"]
    verbs: ["get", "list"]
```

```yaml
# rbac/security-role.yaml -- the namespaced half: Roles/RoleBindings
# (namespace-scoped, so a Role can grant them) and
# PodDisruptionBudgets, lab namespace only
rules:
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["roles", "rolebindings"]
    verbs: ["get", "list"]
  - apiGroups: ["policy"]
    resources: ["poddisruptionbudgets"]
    verbs: ["get", "list"]
```

Pods/Deployments/StatefulSets (container-security + PDB-coverage checks)
are already granted by `role.yaml`/`optimization-role.yaml`; NetworkPolicy
read access is already granted by `network-role.yaml` -- all reused, none
re-granted.

## 9. Persistence

A new `SQLiteSecurityRepository` behind `persistence/interface.py`,
mirroring `SQLiteNetworkRepository`/`SQLiteSchedulingRepository` exactly:
one table for individually-addressable `SecurityFinding` rows, one table
for the latest `SecurityAuditResult` run metadata (mirroring
`optimization_analysis_runs`'s own "only the latest run is kept, as a
freshness marker" shape).

## 10. Observability

One new counter, `kubesentinel_security_findings_total{severity}` --
genuinely new information the existing metrics don't cover (no other
engine's metrics are labeled by severity). One new counter,
`kubesentinel_security_audit_total{result}` (success/partial/failed),
mirroring `optimization_analysis_total`'s own shape for "did the audit
pass itself succeed," distinct from how many findings it produced.

## 11. Testing strategy

- **Unit**: every rule exhaustively, against hand-built typed Kubernetes
  objects (`V1SecurityContext`, `V1ClusterRoleBinding`, etc.) -- each
  rule's positive case, negative case, and (for the RBAC rules) the
  system-subject/system-Role exclusion specifically, since that's the
  one piece of logic most likely to either over-flag noise or silently
  swallow a real finding if it's wrong. `SecurityAuditEngine` determinism
  and `warnings` propagation when a sub-read fails.
- **Integration**: the read-only HTTP-transport check; API route
  validation/error codes.
- **Live**: a new lab scenario exercising at least one container-level
  finding (e.g. a Pod with `privileged: true` or a `hostPath` volume) and
  one RBAC finding (a ServiceAccount bound to `cluster-admin` via a
  deliberately over-permissive binding, in the lab namespace only --
  never touching the cluster's own real bootstrap bindings). The exact
  manifests are decided during implementation and confirmed live, not
  assumed from this doc.

## 12. Known limitations, stated once here rather than per-finding

Everything named "explicitly deferred" in §3 applies cluster-wide, not
per-finding. In addition: this engine's RBAC checks only look at direct
bindings and rules -- they do not resolve what a subject's *effective*
permissions are across every Role/ClusterRole it's bound to combined
(aggregated ClusterRoles, multiple overlapping bindings). A subject could
have broad effective access assembled from several individually-narrow
grants that no single rule here would catch; this is a real, named gap,
not a claim of completeness.

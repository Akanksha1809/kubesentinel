"""Security and configuration auditor models (Milestone 6 of the platform
spec). See docs/SECURITY_AUDITING.md for exactly what's evaluated for
real (container security context, RBAC over-permissiveness, NetworkPolicy
coverage, PodDisruptionBudget coverage, CVE/image vulnerability scanning,
Pod Security Admission namespace labels) vs. what's still out of scope
(ServiceAccount token automount, compliance framework scoring) -- the
first two in that "vs." clause were deferred at this module's original
Milestone 6 scope and have since been closed as later, separate,
explicitly-requested follow-up work; this docstring is corrected to
match, not left describing a state this engine no longer has.

Kept separate from `models/network.py`/`models/scheduling.py` deliberately
-- a `SecurityFinding` has no single owning workload for most of its rule
families (RBAC/NetworkPolicy/PDB findings describe a cluster-scoped or
namespace-scoped fact, not one Pod's own story) and no review lifecycle
the way `ResourceRecommendation` has -- it's a fact about current cluster
state, read-only start to finish.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from kubesentinel.models.completeness import EvidenceCompletenessMixin


class SecuritySeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SecurityResourceRef(BaseModel):
    """Identity of the object one finding is about. `namespace=None` means
    cluster-scoped (ClusterRole/ClusterRoleBinding) -- never the empty
    string, which would misrepresent a cluster-scoped object as being "in
    the empty namespace.\""""

    namespace: str | None = None
    kind: str
    name: str


class SecurityFinding(BaseModel):
    finding_id: str
    rule_id: str
    severity: SecuritySeverity
    resource: SecurityResourceRef
    # The owning workload, when this finding is a per-container/Pod-level
    # finding -- None for RBAC/NetworkPolicy/PDB findings, which have no
    # single owning workload.
    workload_kind: str | None = None
    workload_name: str | None = None
    evidence: list[str] = Field(default_factory=list)
    explanation: str
    remediation: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SecurityAuditResult(EvidenceCompletenessMixin):
    """One `audit()` pass's own metadata plus every finding it produced --
    mirrors `AnalysisRunSummary`'s own "freshness marker, only the latest
    run is kept" shape at the run level, while individual `SecurityFinding`
    rows are separately, individually persisted and addressable (see
    `persistence/security_repo.py`).

    `warnings` (a rule family that could not be evaluated at all, e.g.
    403 listing ClusterRoleBindings, distinct from "evaluated and found
    nothing") is inherited from `EvidenceCompletenessMixin` -- this
    model's own field of that exact name/shape already matched the
    shared vocabulary's own `warnings` field before the mixin existed,
    so adopting it needed no rename. `evidence_completeness` (also
    inherited) is new; see `security/engine.py::SecurityAuditEngine.audit()`
    for where it's set."""

    audit_id: str
    analyzed_at: datetime
    namespaces: list[str] = Field(default_factory=list)
    findings: list[SecurityFinding] = Field(default_factory=list)
    rules_evaluated: list[str] = Field(default_factory=list)

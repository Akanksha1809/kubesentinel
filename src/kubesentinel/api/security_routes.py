"""Security & configuration auditor API routes: read-only cluster-wide
auditing (container security context, RBAC over-permissiveness,
NetworkPolicy coverage, PodDisruptionBudget coverage). No apply endpoint
-- no code anywhere in `security/` ever creates, patches, or deletes
anything; see docs/SECURITY_AUDITING.md Section 7.

A separate `APIRouter`, same pattern as `network_routes.py`/
`scheduling_routes.py` -- `include_router` in `app.py`, depends on
`api/state.py` (not `app.py`) to avoid the same circular-import shape the
other routers already avoid.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from kubesentinel.api.state import state
from kubesentinel.models.security import SecuritySeverity
from kubesentinel.security.service import FindingNotFoundError, SecurityService

router = APIRouter(prefix="/security", tags=["security"])


def _require_security_service() -> SecurityService:
    if state.security is None:
        raise HTTPException(status_code=503, detail="security service unavailable (no cluster connection)")
    return state.security


class AuditRequest(BaseModel):
    # Empty = every watched namespace, mirroring build_workload_inventory()'s
    # own "empty list means all namespaces" convention.
    namespaces: list[str] = Field(default_factory=list)


@router.post("/audit")
def audit(body: AuditRequest | None = None) -> dict:
    """The only endpoint that ever touches the cluster for security
    auditing -- every other route below serves already-persisted findings
    from the last `audit()` call, never a fresh scan."""
    service = _require_security_service()
    try:
        result = service.audit(namespaces=(body.namespaces if body else []))
    except Exception as exc:  # noqa: BLE001 - any failure here means the cluster itself is unreachable
        raise HTTPException(status_code=503, detail=f"audit() failed: {exc}") from exc
    return result.model_dump(mode="json")


@router.get("/findings")
def list_findings(
    namespace: str | None = None,
    severity: SecuritySeverity | None = None,
    rule_id: str | None = None,
) -> list[dict]:
    """`severity` is a typed Enum query param specifically so an invalid
    value gets FastAPI/Pydantic's own 422 rather than silently matching
    nothing and returning `200 []` -- the same fix Milestone 18 made for
    the optimization engine's own category/status filters."""
    findings = _require_security_service().list_findings(
        namespace=namespace, severity=severity.value if severity else None, rule_id=rule_id
    )
    return [f.model_dump(mode="json") for f in findings]


@router.get("/findings/{finding_id}")
def get_finding(finding_id: str) -> dict:
    try:
        return _require_security_service().get_finding(finding_id).model_dump(mode="json")
    except FindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

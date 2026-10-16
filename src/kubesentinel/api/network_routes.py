"""Network troubleshooting engine API routes: read-only connectivity
analysis for a real Pod. No apply endpoint and no active-probe endpoint --
both deliberately out of scope for this milestone; see
docs/NETWORK_DIAGNOSTICS.md Section 2/6.

A separate `APIRouter`, same pattern as `optimization_routes.py`/
`scheduling_routes.py` -- `include_router` in `app.py`, depends on
`api/state.py` (not `app.py`) to avoid the same circular-import shape the
other two routers already avoid.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from kubesentinel.api.state import state
from kubesentinel.models.network import NetworkFindingClassification
from kubesentinel.network.service import FindingNotFoundError, NetworkService, PodNotFoundError

router = APIRouter(prefix="/network", tags=["network"])


def _require_network_service() -> NetworkService:
    if state.network is None:
        raise HTTPException(status_code=503, detail="network service unavailable (no cluster connection)")
    return state.network


class AnalyzePodRequest(BaseModel):
    namespace: str
    pod_name: str


@router.post("/analyze")
def analyze_pod(body: AnalyzePodRequest) -> dict:
    """Explains a real Pod's inbound Service-based connectivity story. The
    only route that touches the cluster -- `GET /findings*` below serve
    already-persisted data."""
    service = _require_network_service()
    try:
        finding = service.analyze_pod(body.namespace, body.pod_name)
    except PodNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - any other failure here means the cluster itself is unreachable
        raise HTTPException(status_code=503, detail=f"analyze() failed: {exc}") from exc
    return finding.model_dump(mode="json")


@router.get("/findings")
def list_findings(namespace: str | None = None, classification: NetworkFindingClassification | None = None) -> list[dict]:
    """`classification` is a typed Enum query param specifically so an
    invalid value gets FastAPI/Pydantic's own 422 rather than silently
    matching nothing and returning `200 []` -- the same fix Milestone 18
    made for the optimization engine's own category/status filters, applied
    here from the start rather than found as a gap later."""
    findings = _require_network_service().list_findings(
        namespace=namespace, classification=classification.value if classification else None
    )
    return [f.model_dump(mode="json") for f in findings]


@router.get("/findings/{finding_id}")
def get_finding(finding_id: str) -> dict:
    try:
        return _require_network_service().get_finding(finding_id).model_dump(mode="json")
    except FindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

"""Scheduling advisor API routes: read-only placement analysis for a real
Pod, or a read-only simulation of a hypothetical one. No apply endpoint --
this engine never creates, patches, or deletes anything; see
docs/SCHEDULING_ANALYSIS.md Section 6.

A separate `APIRouter`, same pattern as `optimization_routes.py` --
`include_router` in `app.py`, depends on `api/state.py` (not `app.py`) to
avoid the same circular-import shape `optimization_routes.py` already
avoids.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from kubesentinel.api.state import state
from kubesentinel.scheduling.service import (
    FindingNotFoundError,
    InvalidPodSpecError,
    PodNotFoundError,
    SchedulingService,
)

router = APIRouter(prefix="/scheduling", tags=["scheduling"])


def _require_scheduling_service() -> SchedulingService:
    if state.scheduling is None:
        raise HTTPException(status_code=503, detail="scheduling service unavailable (no cluster connection)")
    return state.scheduling


class AnalyzePodRequest(BaseModel):
    namespace: str
    pod_name: str


@router.post("/analyze")
def analyze_pod(body: AnalyzePodRequest) -> dict:
    """Analyzes a real, already-existing Pod. The only route (along with
    `/simulate` below) that touches the cluster -- `GET /findings*` below
    serve already-persisted data."""
    service = _require_scheduling_service()
    try:
        finding = service.analyze_pod(body.namespace, body.pod_name)
    except PodNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - any other failure here means the cluster itself is unreachable
        raise HTTPException(status_code=503, detail=f"analyze() failed: {exc}") from exc
    return finding.model_dump(mode="json")


class SimulateRequest(BaseModel):
    namespace: str
    #: A raw Pod spec dict (e.g. the `spec:` block of a Pod manifest) -- not
    #: yet created anywhere. Deserialized into a typed V1PodSpec; an
    #: unrecognized field is rejected as a 422, not silently ignored.
    pod_spec: dict = Field(default_factory=dict)


@router.post("/simulate")
def simulate_pod(body: SimulateRequest) -> dict:
    """Simulates whether a not-yet-created Pod spec would be admitted and
    schedulable -- the only route where the namespace ResourceQuota check
    is meaningful (see `scheduling/analyzer.py`)."""
    service = _require_scheduling_service()
    try:
        finding = service.analyze_hypothetical(body.namespace, body.pod_spec)
    except InvalidPodSpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - any other failure here means the cluster itself is unreachable
        raise HTTPException(status_code=503, detail=f"simulate() failed: {exc}") from exc
    return finding.model_dump(mode="json")


@router.get("/findings")
def list_findings(namespace: str | None = None, schedulable: bool | None = None) -> list[dict]:
    findings = _require_scheduling_service().list_findings(namespace=namespace, schedulable=schedulable)
    return [f.model_dump(mode="json") for f in findings]


@router.get("/findings/{finding_id}")
def get_finding(finding_id: str) -> dict:
    try:
        return _require_scheduling_service().get_finding(finding_id).model_dump(mode="json")
    except FindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

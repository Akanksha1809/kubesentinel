"""Network troubleshooting engine API routes: read-only connectivity
analysis for a real Pod, plus the active connectivity probe
(docs/NETWORK_DIAGNOSTICS.md Section 12) -- opt-in and off by default
(`network_probe_enabled=False`); `/probe` returns 403 rather than running
anything when the feature is disabled or the request is denied.

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
from kubesentinel.network.service import (
    FindingNotFoundError,
    NetworkService,
    PodNotFoundError,
    ProbeDeniedError,
    ProbeDisabledError,
    ProbeTargetError,
)

router = APIRouter(prefix="/network", tags=["network"])


def _require_network_service() -> NetworkService:
    if state.network is None:
        raise HTTPException(status_code=503, detail="network service unavailable (no cluster connection)")
    return state.network


class AnalyzePodRequest(BaseModel):
    namespace: str
    pod_name: str


class ProbeRequest(BaseModel):
    namespace: str
    service_name: str
    port: int | str
    related_finding_id: str | None = None


@router.post("/analyze")
def analyze_pod(body: AnalyzePodRequest) -> dict:
    """Explains a real Pod's inbound Service-based connectivity story. No
    mutation -- `GET /findings*` below serve already-persisted data."""
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


@router.post("/probe")
def run_probe(body: ProbeRequest) -> dict:
    """Creates, waits on, and deletes one short-lived diagnostic Job
    testing DNS resolution and a raw TCP connect against a real Service.
    403 (not 503) means the feature is off or this specific request was
    denied -- distinct from the cluster being unreachable."""
    service = _require_network_service()
    try:
        result = service.run_probe(
            body.namespace, body.service_name, body.port, related_finding_id=body.related_finding_id
        )
    except (ProbeDisabledError, ProbeDeniedError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ProbeTargetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - any other failure here means the cluster itself is unreachable
        raise HTTPException(status_code=503, detail=f"run_probe() failed: {exc}") from exc
    return result.model_dump(mode="json")


@router.get("/probe-results")
def list_probe_results(namespace: str | None = None, service_name: str | None = None, port: int | None = None) -> list[dict]:
    results = _require_network_service().list_probe_results(namespace=namespace, service_name=service_name, port=port)
    return [r.model_dump(mode="json") for r in results]


@router.get("/probe-results/{probe_id}")
def get_probe_result(probe_id: str) -> dict:
    try:
        return _require_network_service().get_probe_result(probe_id).model_dump(mode="json")
    except FindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

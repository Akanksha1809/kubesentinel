"""Optimization engine API routes: read-only analysis, recommendations, and
human review. No apply endpoint exists -- see
RESOURCE_OPTIMIZATION_DESIGN.md §6 for why that's deliberate, not an
oversight: there is no code in this project that patches a workload's
resources, so exposing an endpoint that implied there was would be exactly
the "misleading endpoint for unimplemented behavior" the spec warns against.

A separate `APIRouter` (this project's first) rather than appending ~150
more lines directly into `app.py`, which was already long -- `include_router`
is the standard FastAPI mechanism for this, not a new convention or
dependency. Depends on `api/state.py` (not `app.py`) for `state`/
`require_api_token` specifically to avoid a circular import, since `app.py`
is what imports *this* module.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from kubesentinel.api.state import require_api_token, state
from kubesentinel.models.optimization import RecommendationStatus
from kubesentinel.optimization.service import (
    InvalidRecommendationStateError,
    OptimizationService,
    RecommendationNotFoundError,
)

router = APIRouter(prefix="/optimization", tags=["optimization"])


def _require_optimization_service() -> OptimizationService:
    if state.optimization is None:
        raise HTTPException(status_code=503, detail="optimization service unavailable (no cluster connection)")
    return state.optimization


class AnalyzeRequest(BaseModel):
    # Empty = every watched namespace, mirroring build_workload_inventory()'s
    # own "empty list means all namespaces" convention.
    namespaces: list[str] = Field(default_factory=list)


@router.post("/analyze")
def analyze(body: AnalyzeRequest | None = None) -> dict:
    """The only endpoint that ever touches the cluster for optimization
    purposes -- every other route below serves already-persisted data from
    the last `analyze()` call, never a fresh scan. See
    RESOURCE_OPTIMIZATION_DESIGN.md §4 for why that split is deliberate."""
    service = _require_optimization_service()
    summary = service.analyze(namespaces=(body.namespaces if body else []))
    return summary.model_dump(mode="json")


@router.get("/summary")
def get_summary() -> dict:
    return _require_optimization_service().summary()


@router.get("/workloads")
def list_workloads(namespace: str | None = None) -> list[dict]:
    return [w.model_dump(mode="json") for w in _require_optimization_service().list_workloads(namespace=namespace)]


@router.get("/recommendations")
def list_recommendations(
    namespace: str | None = None,
    workload: str | None = None,
    category: str | None = None,
    status: str | None = None,
) -> list[dict]:
    recommendations = _require_optimization_service().list_recommendations(
        namespace=namespace, workload_name=workload, category=category, status=status
    )
    return [r.model_dump(mode="json") for r in recommendations]


@router.get("/recommendations/{recommendation_id}")
def get_recommendation(recommendation_id: str) -> dict:
    try:
        return _require_optimization_service().get_recommendation(recommendation_id).model_dump(mode="json")
    except RecommendationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ReviewRequest(BaseModel):
    decision: RecommendationStatus
    reviewed_by: str
    notes: str | None = None


@router.post("/recommendations/{recommendation_id}/review", dependencies=[Depends(require_api_token)])
def review_recommendation(recommendation_id: str, body: ReviewRequest) -> dict:
    """Records a human decision -- never applies anything to the cluster.
    Token-gated like the troubleshooting engine's plan/approve/execute/
    reject, even though review itself mutates nothing in the cluster: the
    spec's "require explicit approval for resource changes" is read here as
    the same bar as any other write-ish decision this project records."""
    service = _require_optimization_service()
    try:
        recommendation = service.review(recommendation_id, body.decision, body.reviewed_by, body.notes)
    except RecommendationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidRecommendationStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return recommendation.model_dump(mode="json")

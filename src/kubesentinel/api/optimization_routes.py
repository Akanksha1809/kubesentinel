"""Optimization engine API routes: read-only analysis, recommendations,
human review, and -- as of docs/RESOURCE_OPTIMIZATION_DESIGN.md Section
14 -- applying an already-approved recommendation. `review` still never
touches the cluster; `apply` creates a standalone, policy-gated
RemediationAction (reachable afterward through the sibling
`/actions/{action_id}/...` routes in `app.py`, never a parallel approval
system of its own).

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
from kubesentinel.models.optimization import RecommendationCategory, RecommendationStatus
from kubesentinel.optimization.service import (
    InvalidRecommendationStateError,
    OptimizationService,
    RecommendationNotFoundError,
    RemediationNotConfiguredError,
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
    RESOURCE_OPTIMIZATION_DESIGN.md §4 for why that split is deliberate.

    A merely-missing/unreachable *metrics* provider is not an error here --
    `service.analyze()` already reports that honestly via
    `metrics_available=False` + `warning` in a normal 200 response (see
    `OptimizationService._collect_raw_samples`). This `except` is for the
    cluster itself being unreachable mid-call (confirmed live during this
    milestone's own verification: a transient connectivity failure surfaced
    as a bare, uncaught exception propagating out of `build_workload_inventory`
    and becoming a generic 500) -- RESOURCE_OPTIMIZATION_DESIGN.md §8's own
    error-mapping table promises 503 for exactly this, matching
    `_require_remediation_service()`'s existing "no cluster connection"
    convention."""
    service = _require_optimization_service()
    try:
        summary = service.analyze(namespaces=(body.namespaces if body else []))
    except Exception as exc:  # noqa: BLE001 - any failure here means the cluster itself is unreachable
        raise HTTPException(status_code=503, detail=f"analyze() failed: {exc}") from exc
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
    category: RecommendationCategory | None = None,
    status: RecommendationStatus | None = None,
) -> list[dict]:
    """`category`/`status` are typed Enum query params specifically so an
    invalid value (e.g. `?category=NOT_REAL`) gets FastAPI/Pydantic's own
    422 rather than silently matching nothing and returning `200 []` --
    the design doc's own error-mapping table promises 422 for this, and a
    bad filter value is a client mistake worth surfacing, not a "no results"
    answer indistinguishable from a correct, merely-empty filter."""
    recommendations = _require_optimization_service().list_recommendations(
        namespace=namespace,
        workload_name=workload,
        category=category.value if category else None,
        status=status.value if status else None,
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


class ApplyRequest(BaseModel):
    rationale: str


@router.post("/recommendations/{recommendation_id}/apply", dependencies=[Depends(require_api_token)])
def apply_recommendation(recommendation_id: str, body: ApplyRequest) -> dict:
    """Creates a standalone RemediationAction applying this recommendation's
    own proposed requests/limits -- see docs/RESOURCE_OPTIMIZATION_DESIGN.md
    Section 14.2 for every precondition this can 409 on (not yet APPROVED,
    EXPIRED, carries safety_flags, nothing proposed, not a Deployment,
    already applied). The returned action is always PENDING_APPROVAL in
    practice (ADJUST_RESOURCES never auto-executes) -- a human still calls
    the usual `/actions/{action_id}/approve` + `/execute` on it."""
    service = _require_optimization_service()
    try:
        action = service.apply_recommendation(recommendation_id, body.rationale)
    except RecommendationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidRecommendationStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RemediationNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return action.model_dump(mode="json")

"""FastAPI application: health/readiness, incident inspection, remediation
actions, and Prometheus metrics.

Mutating endpoints (plan/approve/execute/reject) are the first write-capable
surface in the project and are gated by `require_api_token` -- per the spec's
"do not expose an unauthenticated, unrestricted remediation endpoint," these
endpoints refuse every request, not just unauthenticated ones, unless
`KUBESENTINEL_API_TOKEN` is explicitly configured. An empty token means
"mutating endpoints are disabled," never "open."
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from kubesentinel.ai.client import AiClientError, OllamaClient
from kubesentinel.ai.summarizer import summarize_incident
from kubesentinel.config import Settings, get_settings
from kubesentinel.incidents.manager import IncidentManager
from kubesentinel.metrics.registry import ai_errors_total, ai_summaries_total
from kubesentinel.logging_config import configure_logging
from kubesentinel.models.action import ActionType
from kubesentinel.observer.client import build_clients
from kubesentinel.observer.service import ObserverService
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from kubesentinel.policy.engine import PolicyEngine
from kubesentinel.remediation.executor import RemediationExecutor
from kubesentinel.remediation.service import (
    ActionNotFoundError,
    InvalidActionStateError,
    RemediationService,
)
from kubesentinel.verification.verifier import RecoveryVerifier

logger = logging.getLogger(__name__)


class AppState:
    settings: Settings
    repo: SQLiteIncidentRepository
    manager: IncidentManager
    observer: ObserverService | None = None
    remediation: RemediationService | None = None
    ai_client: OllamaClient | None = None


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    state.settings = settings
    state.repo = SQLiteIncidentRepository(settings.db_path)
    state.manager = IncidentManager(state.repo)
    state.manager.reconcile_metrics()
    state.ai_client = (
        OllamaClient(settings.ai_base_url, settings.ai_model, settings.ai_timeout_seconds)
        if settings.ai_enabled
        else None
    )

    try:
        clients = build_clients(settings)
        state.observer = ObserverService(clients, settings, state.manager)
        state.observer.start()
        executor = RemediationExecutor(clients.apps, timeout_seconds=settings.action_execution_timeout_seconds)
        verifier = RecoveryVerifier(
            clients.apps,
            deadline_seconds=settings.verification_deadline_seconds,
            poll_interval_seconds=settings.verification_poll_interval_seconds,
        )
        state.remediation = RemediationService(PolicyEngine(settings), executor, verifier, state.repo, settings)
    except Exception:  # noqa: BLE001 - API must still serve read endpoints without a cluster
        logger.exception("could not start observer; no kubeconfig/in-cluster config available")
        state.observer = None
        state.remediation = None

    yield

    if state.observer:
        state.observer.stop()
    state.repo.close()


app = FastAPI(title="KubeSentinel", version="0.1.0", lifespan=lifespan)


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """Every mutating endpoint depends on this. An unset KUBESENTINEL_API_TOKEN
    means mutation is disabled entirely (503), not left open (never 200) --
    there is no code path here that treats "no token configured" as "no auth
    needed." A configured token that doesn't match the request is 401."""
    if not state.settings.api_token:
        raise HTTPException(
            status_code=503,
            detail="remediation endpoints are disabled: KUBESENTINEL_API_TOKEN is not configured",
        )
    expected = f"Bearer {state.settings.api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def _require_remediation_service() -> RemediationService:
    if state.remediation is None:
        raise HTTPException(status_code=503, detail="remediation service unavailable (no cluster connection)")
    return state.remediation


def _get_incident_or_404(incident_id: str):
    incident = state.repo.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return incident


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    observer_connected = state.observer is not None
    return {"status": "ready" if observer_connected else "degraded", "observer_connected": observer_connected}


@app.get("/incidents")
def list_incidents(status: str | None = None, namespace: str | None = None) -> list[dict]:
    incidents = state.repo.list(status=status, namespace=namespace)
    return [_incident_summary(i) for i in incidents]


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str) -> dict:
    return _get_incident_or_404(incident_id).model_dump(mode="json")


@app.get("/incidents/{incident_id}/evidence")
def get_evidence(incident_id: str) -> list[dict]:
    incident = _get_incident_or_404(incident_id)
    return [e.model_dump(mode="json") for e in incident.evidence]


@app.get("/incidents/{incident_id}/actions")
def get_actions(incident_id: str) -> list[dict]:
    incident = _get_incident_or_404(incident_id)
    return [a.model_dump(mode="json") for a in incident.actions]


@app.get("/incidents/{incident_id}/ai-summary")
def get_ai_summary(incident_id: str) -> dict:
    """Advisory only -- see models/ai.py::AiSummary.disclaimer. Read-only and
    unauthenticated like the other GET endpoints (it mutates nothing); gated
    on `KUBESENTINEL_AI_ENABLED` rather than the mutating-endpoint token,
    since disabling it is about availability/cost, not a security boundary."""
    if state.ai_client is None:
        raise HTTPException(
            status_code=503,
            detail="AI summarization is disabled: set KUBESENTINEL_AI_ENABLED=true "
            "(and have a local Ollama server running) to enable it",
        )
    incident = _get_incident_or_404(incident_id)
    try:
        summary = summarize_incident(incident, state.ai_client, state.settings.ai_model)
    except AiClientError as exc:
        ai_errors_total.inc()
        raise HTTPException(status_code=502, detail=f"AI backend unavailable: {exc}") from exc
    ai_summaries_total.inc()
    return summary.model_dump(mode="json")


@app.post("/incidents/{incident_id}/diagnose")
def diagnose(incident_id: str) -> dict:
    incident = _get_incident_or_404(incident_id)
    result = state.manager.engine.diagnose(incident.evidence)  # re-run on current evidence
    incident.diagnosis_history.append(result)
    state.repo.save(incident)
    return result.model_dump(mode="json")


class ActionPlanRequest(BaseModel):
    action_type: ActionType
    rationale: str
    parameters: dict = Field(default_factory=dict)
    target_desired_replicas: int | None = None
    #: Preview the policy decision with zero side effects -- no action is built,
    #: persisted, or executed. The CLI's `dry-run` command is this flag set True.
    dry_run: bool = False


@app.post("/incidents/{incident_id}/actions/plan", dependencies=[Depends(require_api_token)])
def plan_action(incident_id: str, body: ActionPlanRequest) -> dict:
    incident = _get_incident_or_404(incident_id)
    service = _require_remediation_service()
    if body.dry_run:
        decision = service.dry_run(
            incident, body.action_type, body.parameters, body.target_desired_replicas
        )
        return {"dry_run": True, "outcome": decision.outcome.value, "reason": decision.reason}
    action = service.propose(
        incident, body.action_type, body.rationale, body.parameters, body.target_desired_replicas
    )
    return action.model_dump(mode="json")


class ApproveRequest(BaseModel):
    approved_by: str


@app.post(
    "/incidents/{incident_id}/actions/{action_id}/approve", dependencies=[Depends(require_api_token)]
)
def approve_action(incident_id: str, action_id: str, body: ApproveRequest) -> dict:
    incident = _get_incident_or_404(incident_id)
    service = _require_remediation_service()
    try:
        action = service.approve(incident, action_id, body.approved_by)
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidActionStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return action.model_dump(mode="json")


@app.post(
    "/incidents/{incident_id}/actions/{action_id}/execute", dependencies=[Depends(require_api_token)]
)
def execute_action(incident_id: str, action_id: str) -> dict:
    incident = _get_incident_or_404(incident_id)
    service = _require_remediation_service()
    try:
        action = service.execute(incident, action_id)
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidActionStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return action.model_dump(mode="json")


class RejectRequest(BaseModel):
    rejected_by: str
    reason: str


@app.post(
    "/incidents/{incident_id}/actions/{action_id}/reject", dependencies=[Depends(require_api_token)]
)
def reject_action(incident_id: str, action_id: str, body: RejectRequest) -> dict:
    incident = _get_incident_or_404(incident_id)
    service = _require_remediation_service()
    try:
        action = service.reject(incident, action_id, body.rejected_by, body.reason)
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidActionStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return action.model_dump(mode="json")


@app.get("/cluster/summary")
def cluster_summary() -> dict:
    incidents = state.repo.list()
    open_incidents = [i for i in incidents if i.status.value != "RESOLVED"]
    by_category: dict[str, int] = {}
    for i in open_incidents:
        by_category[i.category] = by_category.get(i.category, 0) + 1
    return {
        "total_incidents": len(incidents),
        "open_incidents": len(open_incidents),
        "open_by_category": by_category,
        "observer_connected": state.observer is not None,
    }


@app.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _incident_summary(incident) -> dict:
    return {
        "incident_id": incident.incident_id,
        "namespace": incident.resource.namespace,
        "resource_kind": incident.resource.kind,
        "resource_name": incident.resource.name,
        "category": incident.category,
        "severity": incident.severity.value,
        "status": incident.status.value,
        "first_observed_at": incident.first_observed_at.isoformat(),
        "last_observed_at": incident.last_observed_at.isoformat(),
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        "recovery_duration_seconds": incident.recovery_duration_seconds,
    }

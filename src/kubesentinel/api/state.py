"""Shared app state and the auth dependency every mutating endpoint uses --
split out from `app.py` specifically so `optimization_routes.py` (a separate
`APIRouter`, not defined in `app.py` itself) can depend on the same
`require_api_token` and the same live service instances without a circular
import of `app.py`.
"""

from __future__ import annotations

from fastapi import Header, HTTPException

from kubesentinel.ai.client import OllamaClient
from kubesentinel.config import Settings
from kubesentinel.incidents.manager import IncidentManager
from kubesentinel.observer.service import ObserverService
from kubesentinel.optimization.service import OptimizationService
from kubesentinel.persistence.optimization_repo import SQLiteOptimizationRepository
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from kubesentinel.remediation.service import RemediationService


class AppState:
    settings: Settings
    repo: SQLiteIncidentRepository
    manager: IncidentManager
    observer: ObserverService | None = None
    remediation: RemediationService | None = None
    ai_client: OllamaClient | None = None
    optimization_repo: SQLiteOptimizationRepository
    optimization: OptimizationService | None = None


state = AppState()


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """Every mutating endpoint across the whole API depends on this -- both
    the troubleshooting engine's plan/approve/execute/reject and the
    optimization engine's review. An unset `KUBESENTINEL_API_TOKEN` means
    mutation is disabled entirely (503), not left open (never 200) -- there
    is no code path here that treats "no token configured" as "no auth
    needed." A configured token that doesn't match the request is 401."""
    if not state.settings.api_token:
        raise HTTPException(
            status_code=503,
            detail="mutating endpoints are disabled: KUBESENTINEL_API_TOKEN is not configured",
        )
    expected = f"Bearer {state.settings.api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")

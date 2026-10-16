"""Chaos & resilience evaluation harness API routes: read-only access to
past runs only. No `POST /evaluation/run` -- unlike `/optimization/analyze`,
`/security/audit`, etc., running a battery mutates the shared lab namespace
and can take several minutes (see docs/CHAOS_RESILIENCE_LAB.md §8); it is
CLI-only (`kubesentinel evaluate`), the same boundary that already keeps
`lab/scripts/*.sh` out of the HTTP surface entirely.

Same separate-`APIRouter` pattern as every other capability's routes file.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from kubesentinel.api.state import state

router = APIRouter(prefix="/evaluation", tags=["evaluation"])


@router.get("/runs")
def list_runs() -> list[dict]:
    return [run.model_dump(mode="json") for run in state.evaluation_repo.list_runs()]


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = state.evaluation_repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="evaluation run not found")
    return run.model_dump(mode="json")

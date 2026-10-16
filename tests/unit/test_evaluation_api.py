"""API tests for the evaluation harness's two read-only routes -- same
"bypass lifespan, set state directly" pattern as test_security_api.py.
There is deliberately no `POST /evaluation/run` to test: see
docs/CHAOS_RESILIENCE_LAB.md §8 for why that endpoint does not exist.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kubesentinel.api import app as app_module
from kubesentinel.models.evaluation import DetectionOutcome, EvaluationCapability, EvaluationRun, ScenarioResult
from kubesentinel.persistence.evaluation_repo import SQLiteEvaluationRepository

client = TestClient(app_module.app)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteEvaluationRepository(str(Path(d) / "test.db"))
        app_module.state.evaluation_repo = r
        yield r
        r.close()


def _run() -> EvaluationRun:
    return EvaluationRun(
        run_id=str(uuid4()),
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=30),
        scenario_results=[
            ScenarioResult(
                scenario_name="oom",
                capability=EvaluationCapability.TROUBLESHOOTING,
                applied_at=NOW,
                outcome=DetectionOutcome.DETECTED,
                observed="OOM_KILLED",
                detail="incident diagnosed OOM_KILLED",
            )
        ],
        false_positive_count=0,
        total_open_findings_count=1,
        false_positive_rate=0.0,
    )


def test_list_runs_empty(repo):
    response = client.get("/evaluation/runs")
    assert response.status_code == 200
    assert response.json() == []


def test_list_runs_after_a_save(repo):
    run = _run()
    repo.save_run(run)

    response = client.get("/evaluation/runs")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["run_id"] == run.run_id


def test_get_run_by_id(repo):
    run = _run()
    repo.save_run(run)

    response = client.get(f"/evaluation/runs/{run.run_id}")
    assert response.status_code == 200
    assert response.json()["scenario_results"][0]["scenario_name"] == "oom"


def test_get_run_not_found_is_404(repo):
    response = client.get("/evaluation/runs/does-not-exist")
    assert response.status_code == 404

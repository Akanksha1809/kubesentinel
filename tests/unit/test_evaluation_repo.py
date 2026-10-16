import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from kubesentinel.models.evaluation import DetectionOutcome, EvaluationCapability, EvaluationRun, ScenarioResult
from kubesentinel.persistence.evaluation_repo import SQLiteEvaluationRepository

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteEvaluationRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _run(started_at: datetime = NOW, **overrides) -> EvaluationRun:
    defaults = dict(
        run_id=str(uuid4()),
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=30),
        scenario_results=[
            ScenarioResult(
                scenario_name="oom",
                capability=EvaluationCapability.TROUBLESHOOTING,
                applied_at=started_at,
                outcome=DetectionOutcome.DETECTED,
                time_to_detection_seconds=4.2,
                time_to_diagnosis_seconds=4.2,
                observed="OOM_KILLED",
                detail="incident diagnosed OOM_KILLED",
            )
        ],
        false_positive_count=0,
        total_open_findings_count=5,
        false_positive_rate=0.0,
    )
    defaults.update(overrides)
    return EvaluationRun(**defaults)


def test_save_then_get_roundtrips(repo):
    run = _run()
    repo.save_run(run)

    fetched = repo.get_run(run.run_id)
    assert fetched == run


def test_get_missing_run_returns_none(repo):
    assert repo.get_run("does-not-exist") is None


def test_list_runs_orders_most_recent_first(repo):
    older = _run(started_at=NOW)
    newer = _run(started_at=NOW + timedelta(hours=1))
    repo.save_run(older)
    repo.save_run(newer)

    runs = repo.list_runs()
    assert [r.run_id for r in runs] == [newer.run_id, older.run_id]


def test_save_run_is_an_upsert_by_run_id(repo):
    run = _run()
    repo.save_run(run)

    updated = run.model_copy(update={"false_positive_count": 3})
    repo.save_run(updated)

    assert repo.get_run(run.run_id).false_positive_count == 3
    assert len(repo.list_runs()) == 1

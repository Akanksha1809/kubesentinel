"""SQLite-backed EvaluationRepository.

One row per `EvaluationRun`, serialized as a JSON blob -- the same "whole
run, not an upsert target" shape as `security_repo.py`, except here each
run gets its own permanent row (`run_id`) rather than replacing the
previous one: unlike a security audit, an evaluation run's entire purpose
is comparing against *past* runs (docs/CHAOS_RESILIENCE_LAB.md), so
history must be kept, not replaced.
"""

from __future__ import annotations

import builtins
import sqlite3
import threading

from kubesentinel.models.evaluation import EvaluationRun
from kubesentinel.persistence.interface import EvaluationRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluation_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_started_at ON evaluation_runs(started_at);
"""


class SQLiteEvaluationRepository(EvaluationRepository):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def save_run(self, run: EvaluationRun) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO evaluation_runs (run_id, started_at, data) VALUES (?, ?, ?)",
                (run.run_id, run.started_at.isoformat(), run.model_dump_json()),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> EvaluationRun | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM evaluation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return EvaluationRun.model_validate_json(row[0]) if row else None

    def list_runs(self) -> builtins.list[EvaluationRun]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM evaluation_runs ORDER BY started_at DESC"
            ).fetchall()
        return [EvaluationRun.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

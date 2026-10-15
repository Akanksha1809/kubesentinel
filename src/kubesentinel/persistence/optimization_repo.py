"""SQLite-backed OptimizationRepository.

Same shape as `sqlite_repo.py::SQLiteIncidentRepository` -- one row per
object, the object serialized as a JSON blob, a few indexed columns for the
filters actually used, a per-instance lock since sqlite3 connections aren't
safe to share across threads without one. Deliberately its own connection
to the same `db_path`, not a shared one with `SQLiteIncidentRepository`:
each repository manages its own tables and its own lock, so the two engines
never contend on the same in-process lock for unrelated work, and either
could be swapped to a different backend independently later.
"""

from __future__ import annotations

import builtins
import sqlite3
import threading

from kubesentinel.models.optimization import AnalysisRunSummary, ResourceRecommendation, WorkloadSnapshot
from kubesentinel.persistence.interface import OptimizationRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS optimization_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    workload_name TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_optimization_recommendations_namespace
    ON optimization_recommendations(namespace);
CREATE INDEX IF NOT EXISTS idx_optimization_recommendations_workload_name
    ON optimization_recommendations(workload_name);
CREATE INDEX IF NOT EXISTS idx_optimization_recommendations_category
    ON optimization_recommendations(category);
CREATE INDEX IF NOT EXISTS idx_optimization_recommendations_status
    ON optimization_recommendations(status);

CREATE TABLE IF NOT EXISTS optimization_workload_snapshots (
    workload_uid TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_optimization_workload_snapshots_namespace
    ON optimization_workload_snapshots(namespace);

CREATE TABLE IF NOT EXISTS optimization_analysis_runs (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
"""

_LATEST_ANALYSIS_RUN_ID = "latest"


class SQLiteOptimizationRepository(OptimizationRepository):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get_recommendation(self, recommendation_id: str) -> ResourceRecommendation | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM optimization_recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
        return ResourceRecommendation.model_validate_json(row[0]) if row else None

    def save_recommendation(self, recommendation: ResourceRecommendation) -> None:
        data = recommendation.model_dump_json()
        now = recommendation.created_at.isoformat()
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at FROM optimization_recommendations WHERE recommendation_id = ?",
                (recommendation.recommendation_id,),
            ).fetchone()
            created_at = existing[0] if existing else now
            self._conn.execute(
                """
                INSERT INTO optimization_recommendations
                    (recommendation_id, namespace, workload_name, category, status, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recommendation_id) DO UPDATE SET
                    namespace=excluded.namespace,
                    workload_name=excluded.workload_name,
                    category=excluded.category,
                    status=excluded.status,
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (
                    recommendation.recommendation_id,
                    recommendation.namespace,
                    recommendation.workload_name,
                    recommendation.category.value,
                    recommendation.status.value,
                    data,
                    created_at,
                    now,
                ),
            )
            self._conn.commit()

    def list_recommendations(
        self,
        *,
        namespace: str | None = None,
        workload_name: str | None = None,
        category: str | None = None,
        status: str | None = None,
    ) -> builtins.list[ResourceRecommendation]:
        query = "SELECT data FROM optimization_recommendations WHERE 1=1"
        params: list[str] = []
        if namespace:
            query += " AND namespace = ?"
            params.append(namespace)
        if workload_name:
            query += " AND workload_name = ?"
            params.append(workload_name)
        if category:
            query += " AND category = ?"
            params.append(category)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [ResourceRecommendation.model_validate_json(r[0]) for r in rows]

    def save_workload_snapshots(self, snapshots: builtins.list[WorkloadSnapshot]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM optimization_workload_snapshots")
            self._conn.executemany(
                "INSERT INTO optimization_workload_snapshots (workload_uid, namespace, data) VALUES (?, ?, ?)",
                [(s.workload_uid, s.namespace, s.model_dump_json()) for s in snapshots],
            )
            self._conn.commit()

    def list_workload_snapshots(self, *, namespace: str | None = None) -> builtins.list[WorkloadSnapshot]:
        query = "SELECT data FROM optimization_workload_snapshots"
        params: list[str] = []
        if namespace:
            query += " WHERE namespace = ?"
            params.append(namespace)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [WorkloadSnapshot.model_validate_json(r[0]) for r in rows]

    def save_analysis_run(self, summary: AnalysisRunSummary) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO optimization_analysis_runs (id, data) VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET data=excluded.data
                """,
                (_LATEST_ANALYSIS_RUN_ID, summary.model_dump_json()),
            )
            self._conn.commit()

    def get_latest_analysis_run(self) -> AnalysisRunSummary | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM optimization_analysis_runs WHERE id = ?", (_LATEST_ANALYSIS_RUN_ID,)
            ).fetchone()
        return AnalysisRunSummary.model_validate_json(row[0]) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

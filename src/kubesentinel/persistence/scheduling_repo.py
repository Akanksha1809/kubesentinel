"""SQLite-backed SchedulingRepository.

Same shape as `optimization_repo.py::SQLiteOptimizationRepository` -- one
row per finding, serialized as a JSON blob, a few indexed columns for the
filters actually used, a per-instance lock, its own connection to the same
`db_path` (never shared with the other two repositories, for the same
"each engine's own lock, independently swappable later" reasoning
`optimization_repo.py` already states).
"""

from __future__ import annotations

import builtins
import sqlite3
import threading

from kubesentinel.models.scheduling import SchedulingFinding
from kubesentinel.persistence.interface import SchedulingRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduling_findings (
    finding_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    schedulable TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduling_findings_namespace ON scheduling_findings(namespace);
CREATE INDEX IF NOT EXISTS idx_scheduling_findings_schedulable ON scheduling_findings(schedulable);
"""


class SQLiteSchedulingRepository(SchedulingRepository):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get_finding(self, finding_id: str) -> SchedulingFinding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM scheduling_findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
        return SchedulingFinding.model_validate_json(row[0]) if row else None

    def save_finding(self, finding: SchedulingFinding) -> None:
        data = finding.model_dump_json()
        now = finding.created_at.isoformat()
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at FROM scheduling_findings WHERE finding_id = ?", (finding.finding_id,)
            ).fetchone()
            created_at = existing[0] if existing else now
            self._conn.execute(
                """
                INSERT INTO scheduling_findings
                    (finding_id, namespace, schedulable, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(finding_id) DO UPDATE SET
                    namespace=excluded.namespace,
                    schedulable=excluded.schedulable,
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (
                    finding.finding_id,
                    finding.namespace,
                    str(finding.any_node_schedulable),
                    data,
                    created_at,
                    now,
                ),
            )
            self._conn.commit()

    def list_findings(
        self, *, namespace: str | None = None, schedulable: bool | None = None
    ) -> builtins.list[SchedulingFinding]:
        query = "SELECT data FROM scheduling_findings WHERE 1=1"
        params: list[str] = []
        if namespace:
            query += " AND namespace = ?"
            params.append(namespace)
        if schedulable is not None:
            query += " AND schedulable = ?"
            params.append(str(schedulable))
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [SchedulingFinding.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

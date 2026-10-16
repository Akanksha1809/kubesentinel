"""SQLite-backed NetworkRepository.

Same shape as `scheduling_repo.py::SQLiteSchedulingRepository`: one row per
finding, serialized as a JSON blob, a few indexed columns for the filters
actually used, a per-instance lock, its own connection to the same
`db_path`.
"""

from __future__ import annotations

import builtins
import sqlite3
import threading

from kubesentinel.models.network import NetworkFinding
from kubesentinel.persistence.interface import NetworkRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS network_findings (
    finding_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    classification TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_network_findings_namespace ON network_findings(namespace);
CREATE INDEX IF NOT EXISTS idx_network_findings_classification ON network_findings(classification);
"""


class SQLiteNetworkRepository(NetworkRepository):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get_finding(self, finding_id: str) -> NetworkFinding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM network_findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
        return NetworkFinding.model_validate_json(row[0]) if row else None

    def save_finding(self, finding: NetworkFinding) -> None:
        data = finding.model_dump_json()
        now = finding.created_at.isoformat()
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at FROM network_findings WHERE finding_id = ?", (finding.finding_id,)
            ).fetchone()
            created_at = existing[0] if existing else now
            self._conn.execute(
                """
                INSERT INTO network_findings
                    (finding_id, namespace, classification, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(finding_id) DO UPDATE SET
                    namespace=excluded.namespace,
                    classification=excluded.classification,
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (
                    finding.finding_id,
                    finding.namespace,
                    finding.classification.value,
                    data,
                    created_at,
                    now,
                ),
            )
            self._conn.commit()

    def list_findings(
        self, *, namespace: str | None = None, classification: str | None = None
    ) -> builtins.list[NetworkFinding]:
        query = "SELECT data FROM network_findings WHERE 1=1"
        params: list[str] = []
        if namespace:
            query += " AND namespace = ?"
            params.append(namespace)
        if classification:
            query += " AND classification = ?"
            params.append(classification)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [NetworkFinding.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

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

from kubesentinel.models.network import ConnectivityProbeResult, NetworkFinding
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

-- Section 12: the active connectivity probe's own results -- a sibling
-- table in the same file, not a new repository class (same capability,
-- same package). Insert-only: a probe result is never upserted.
CREATE TABLE IF NOT EXISTS connectivity_probe_results (
    probe_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    service_name TEXT NOT NULL,
    port INTEGER NOT NULL,
    data TEXT NOT NULL,
    requested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probe_results_namespace ON connectivity_probe_results(namespace);
CREATE INDEX IF NOT EXISTS idx_probe_results_target ON connectivity_probe_results(namespace, service_name, port);
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

    def get_probe_result(self, probe_id: str) -> ConnectivityProbeResult | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM connectivity_probe_results WHERE probe_id = ?", (probe_id,)
            ).fetchone()
        return ConnectivityProbeResult.model_validate_json(row[0]) if row else None

    def save_probe_result(self, result: ConnectivityProbeResult) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO connectivity_probe_results
                    (probe_id, namespace, service_name, port, data, requested_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.probe_id,
                    result.namespace,
                    result.service_name,
                    result.port,
                    result.model_dump_json(),
                    result.requested_at.isoformat(),
                ),
            )
            self._conn.commit()

    def list_probe_results(
        self, *, namespace: str | None = None, service_name: str | None = None, port: int | None = None
    ) -> builtins.list[ConnectivityProbeResult]:
        query = "SELECT data FROM connectivity_probe_results WHERE 1=1"
        params: list[str | int] = []
        if namespace:
            query += " AND namespace = ?"
            params.append(namespace)
        if service_name:
            query += " AND service_name = ?"
            params.append(service_name)
        if port is not None:
            query += " AND port = ?"
            params.append(port)
        query += " ORDER BY requested_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [ConnectivityProbeResult.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

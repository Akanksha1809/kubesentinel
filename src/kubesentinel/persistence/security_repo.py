"""SQLite-backed SecurityRepository.

One row per finding, serialized as a JSON blob, a few indexed columns for
the filters actually used, a per-instance lock, its own connection to the
same `db_path`. `namespace` is nullable here (unlike `NetworkFinding`'s
required one) -- a RBAC finding about a ClusterRole/ClusterRoleBinding is
genuinely cluster-scoped, never "in" some namespace.

`save_findings` deletes every previously-persisted row before inserting
the fresh set, mirroring `optimization_repo.py::save_workload_snapshots`
-- never an incremental upsert; see `SecurityRepository`'s own docstring
for why.
"""

from __future__ import annotations

import builtins
import sqlite3
import threading

from kubesentinel.models.security import SecurityFinding
from kubesentinel.persistence.interface import SecurityRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS security_findings (
    finding_id TEXT PRIMARY KEY,
    namespace TEXT,
    severity TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_security_findings_namespace ON security_findings(namespace);
CREATE INDEX IF NOT EXISTS idx_security_findings_severity ON security_findings(severity);
CREATE INDEX IF NOT EXISTS idx_security_findings_rule_id ON security_findings(rule_id);
"""


class SQLiteSecurityRepository(SecurityRepository):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get_finding(self, finding_id: str) -> SecurityFinding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM security_findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
        return SecurityFinding.model_validate_json(row[0]) if row else None

    def save_findings(self, findings: builtins.list[SecurityFinding]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM security_findings")
            self._conn.executemany(
                """
                INSERT INTO security_findings
                    (finding_id, namespace, severity, rule_id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        finding.finding_id,
                        finding.resource.namespace,
                        finding.severity.value,
                        finding.rule_id,
                        finding.model_dump_json(),
                        finding.created_at.isoformat(),
                        finding.created_at.isoformat(),
                    )
                    for finding in findings
                ],
            )
            self._conn.commit()

    def list_findings(
        self, *, namespace: str | None = None, severity: str | None = None, rule_id: str | None = None
    ) -> builtins.list[SecurityFinding]:
        query = "SELECT data FROM security_findings WHERE 1=1"
        params: list[str] = []
        if namespace:
            query += " AND namespace = ?"
            params.append(namespace)
        if severity:
            query += " AND severity = ?"
            params.append(severity)
        if rule_id:
            query += " AND rule_id = ?"
            params.append(rule_id)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [SecurityFinding.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

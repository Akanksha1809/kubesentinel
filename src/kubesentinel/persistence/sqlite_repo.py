"""SQLite-backed IncidentRepository.

Each incident is stored as one row with the full Incident serialized as JSON. This is
deliberately simple for a local, single-process tool: no ORM, no migrations framework.
Writes are wrapped in a transaction per call, and a per-instance lock serializes access
since sqlite3 connections are not safe to share across threads without one.
"""

from __future__ import annotations

import builtins
import sqlite3
import threading
from datetime import datetime, timezone

from kubesentinel.models.action import RemediationAction
from kubesentinel.models.incident import Incident
from kubesentinel.persistence.interface import IncidentRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    resource_uid TEXT NOT NULL,
    namespace TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_resource_uid ON incidents(resource_uid);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_namespace ON incidents(namespace);

-- RemediationActions with no owning Incident (docs/RESOURCE_OPTIMIZATION_DESIGN.md
-- Section 14.1/14.5) -- a sibling table in the same file, not a new
-- repository interface: this repository is already the action-history
-- authority for the policy engine's per-window budget.
CREATE TABLE IF NOT EXISTS standalone_actions (
    action_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_RESOLVED_STATUSES = {"RESOLVED"}


class SQLiteIncidentRepository(IncidentRepository):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get(self, incident_id: str) -> Incident | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
        if row is None:
            return None
        return Incident.model_validate_json(row[0])

    def find_open_by_resource_uid(self, resource_uid: str) -> Incident | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM incidents WHERE resource_uid = ? "
                "AND status NOT IN ('RESOLVED', 'SUPERSEDED') "
                "ORDER BY updated_at DESC",
                (resource_uid,),
            ).fetchall()
        for (data,) in rows:
            return Incident.model_validate_json(data)
        return None

    def save(self, incident: Incident) -> None:
        data = incident.model_dump_json()
        now = incident.last_observed_at.isoformat()
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at FROM incidents WHERE incident_id = ?", (incident.incident_id,)
            ).fetchone()
            created_at = existing[0] if existing else incident.first_observed_at.isoformat()
            self._conn.execute(
                """
                INSERT INTO incidents (incident_id, resource_uid, namespace, status, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    resource_uid=excluded.resource_uid,
                    namespace=excluded.namespace,
                    status=excluded.status,
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (
                    incident.incident_id,
                    incident.resource.uid,
                    incident.resource.namespace,
                    incident.status.value,
                    data,
                    created_at,
                    now,
                ),
            )
            self._conn.commit()

    def list(self, *, status: str | None = None, namespace: str | None = None) -> list[Incident]:
        query = "SELECT data FROM incidents WHERE 1=1"
        params: list[str] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if namespace:
            query += " AND namespace = ?"
            params.append(namespace)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [Incident.model_validate_json(r[0]) for r in rows]

    def list_actions_since(self, since: datetime) -> builtins.list[RemediationAction]:
        # Simple, not indexed on action.proposed_at: scans every incident's own
        # actions list, plus every standalone action. Correct and fast enough
        # for a local, single-process tool with a bounded number of incidents
        # -- matches this project's existing SQLite design philosophy (see
        # DESIGN_DECISIONS.md).
        actions: list[RemediationAction] = []
        for incident in self.list():
            actions.extend(a for a in incident.actions if a.proposed_at >= since)
        with self._lock:
            rows = self._conn.execute("SELECT data FROM standalone_actions").fetchall()
        actions.extend(
            action
            for action in (RemediationAction.model_validate_json(r[0]) for r in rows)
            if action.proposed_at >= since
        )
        return actions

    def get_standalone_action(self, action_id: str) -> RemediationAction | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM standalone_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        return RemediationAction.model_validate_json(row[0]) if row else None

    def save_standalone_action(self, action: RemediationAction) -> None:
        data = action.model_dump_json()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO standalone_actions (action_id, data, proposed_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (action.action_id, data, action.proposed_at.isoformat(), now),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

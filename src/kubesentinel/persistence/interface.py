"""Repository interface for incident persistence.

Kept storage-agnostic so SQLite (the default, zero-ops local store) can be swapped for
PostgreSQL later without touching the incident manager, API, or CLI.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from datetime import datetime

from kubesentinel.models.action import RemediationAction
from kubesentinel.models.incident import Incident


class IncidentRepository(ABC):
    @abstractmethod
    def get(self, incident_id: str) -> Incident | None: ...

    @abstractmethod
    def find_open_by_resource_uid(self, resource_uid: str) -> Incident | None:
        """Find a non-resolved incident already tracking this exact resource UID."""
        ...

    @abstractmethod
    def save(self, incident: Incident) -> None:
        """Upsert. Must be atomic: concurrent saves of the same incident_id never corrupt state."""
        ...

    @abstractmethod
    def list(
        self, *, status: str | None = None, namespace: str | None = None
    ) -> list[Incident]: ...

    @abstractmethod
    def list_actions_since(self, since: datetime) -> builtins.list[RemediationAction]:
        """Every RemediationAction proposed at or after `since`, across all incidents.

        Used for the policy engine's per-window action budget -- a global rate
        limit independent of which incident each action belongs to.

        Return type is spelled `builtins.list[...]` rather than `list[...]` --
        this class already defines a method named `list` above, which shadows the
        builtin name within this class body's annotation scope.
        """
        ...

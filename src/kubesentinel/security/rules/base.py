"""Security rule interface.

Every rule is a pure function of the already-fetched `SecurityInventory`:
no network calls, no side effects, no randomness. Mirrors
`diagnostics/rules/base.py::DiagnosticRule` exactly, applied to a new
domain -- same reasoning, same testability guarantee.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kubesentinel.models.security import SecurityFinding
from kubesentinel.security.inventory import SecurityInventory


class SecurityRule(ABC):
    rule_id: str

    @abstractmethod
    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        """Return zero or more findings supported by the given inventory.

        Returning an empty list means this rule found nothing -- never
        that it couldn't check (a sub-read failure is recorded in
        `SecurityInventory.warnings` by the inventory builder itself, not
        by returning a fabricated finding here).
        """
        raise NotImplementedError

"""Diagnostic rule interface.

Every rule is a pure function of the evidence collected so far: no network calls, no
side effects, no randomness. This is what makes rules exhaustively unit-testable and
keeps the diagnostic engine deterministic -- same evidence in, same hypotheses out.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kubesentinel.models.diagnosis import Hypothesis
from kubesentinel.models.evidence import Evidence


class DiagnosticRule(ABC):
    name: str

    @abstractmethod
    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        """Return zero or more hypotheses supported by the given evidence.

        Returning an empty list means this rule found nothing relevant -- not that it
        ruled out its failure mode. The engine treats "no rule matched" as inconclusive.
        """
        raise NotImplementedError

"""Diagnostic rule interface.

Every rule is a pure function of the evidence collected so far: no network calls, no
side effects, no randomness. This is what makes rules exhaustively unit-testable and
keeps the diagnostic engine deterministic -- same evidence in, same hypotheses out.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kubesentinel.models.diagnosis import Hypothesis
from kubesentinel.models.evidence import Evidence, EvidenceKind


class DiagnosticRule(ABC):
    name: str

    @abstractmethod
    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        """Return zero or more hypotheses supported by the given evidence.

        Returning an empty list means this rule found nothing relevant -- not that it
        ruled out its failure mode. The engine treats "no rule matched" as inconclusive.
        """
        raise NotImplementedError


def latest_by_source_field(evidence: list[Evidence], kind: EvidenceKind) -> list[Evidence]:
    """One entry per distinct `source_field` of the given `kind` -- whichever
    is latest in `evidence`'s own append order (always chronological:
    `IncidentManager` only ever appends, never reorders or replaces).

    `source_field` identifies one specific, current-state-shaped fact (e.g.
    one container's `state.waiting`, keyed by container name already) --
    `IncidentManager` never supersedes an older observation of the same
    field when a newer one arrives (several rules genuinely need that full
    history, e.g. `UnsatisfiedReplicasRule`'s sustained-deficit timing
    across repeated `status.replicas` snapshots), so a rule whose own
    semantics are "is this CURRENTLY true" must filter down to the latest
    observation of each field itself -- "was ever observed" is not the
    same claim as "is true now."

    Found live: an incident that genuinely transitioned CrashLoopBackOff ->
    ErrImagePull (the Pod's image was fixed mid-crash-loop) stayed
    misdiagnosed as CrashLoopBackOff, because the rule that should have
    stopped matching scanned the entire accumulated evidence history for
    "any entry, ever" rather than "what does the latest observation show."
    `ImagePullRule`/`OOMKilledRule`/`MissingConfigReferenceRule` each had
    this shape; `CrashLoopBackOffRule`'s own selector already restricts to
    the exact current value it cares about."""
    latest: dict[str, Evidence] = {}
    for e in evidence:
        if e.kind == kind:
            latest[e.source_field] = e
    return list(latest.values())

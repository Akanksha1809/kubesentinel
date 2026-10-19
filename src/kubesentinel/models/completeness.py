"""The shared evidence-completeness vocabulary `docs/MASTER_ARCHITECTURE.md`
left as an explicit, undecided question: "there is no shared three-state
'healthy / degraded / unknown-because-unavailable' model across
capabilities today... worth deciding whether this becomes a shared, typed
enum... or stays per-capability by deliberate choice. Not decided by this
document -- flagged for the milestone that first needs it." All six
capabilities now exist, and -- exactly as that note predicted -- each had
independently invented its own version: `AnalysisRunSummary.metrics_available`
+ `.warning` (a singular string), `SecurityAuditResult.warnings` (a list,
no completeness verdict alongside it), and `SchedulingFinding`/`NetworkFinding`
with no equivalent at all. This module is the decision: a shared, typed
enum, included via a common base model, closing the question rather than
leaving a fifth and sixth capability to invent their own too.

Named `completeness.py`, deliberately not `evidence.py` -- that name
already belongs to `models/evidence.py::Evidence`/`EvidenceKind`, the
troubleshooting engine's own foundational "an observed fact pulled from
the API" model since Milestone 1, an unrelated concept this module must
never be confused with.

Deliberately additive, never a replacement: every existing per-capability
field (`AnalysisRunSummary.warning`, `SecurityAuditResult`'s own
pre-existing `warnings`) is kept exactly as it was -- this gives every
model one more, shared, cross-capability-comparable way to say the same
thing, not a breaking rename. See docs/EVIDENCE_COMPLETENESS.md for the
full design and which models this round of the decision actually touches
(not all of them -- named explicitly, not silently assumed complete).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class EvidenceCompleteness(str, Enum):
    """The one-glance verdict a caller can read without first learning
    each capability's own bespoke scheme. Four states, matching
    `MASTER_ARCHITECTURE.md`'s own named three ("degraded / unknown /
    not applicable") plus the implicit positive case it was contrasted
    against:

    - `COMPLETE`: every read this pass attempted succeeded; nothing
      named in `warnings`.
    - `DEGRADED`: the pass still produced a usable result, but at least
      one sub-read failed or fell back -- see `warnings` for which.
    - `UNKNOWN`: no meaningful result could be produced at all (the
      relevant backend was entirely unreachable, not just one sub-read
      among several).
    - `NOT_APPLICABLE`: this check's own precondition doesn't hold for
      the target at all (e.g. a resource with no limit set, for a
      throttling check that only means something when one exists) --
      never conflated with `UNKNOWN`, which means "could not tell,"
      not "the question doesn't apply here."
    """

    COMPLETE = "COMPLETE"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceCompletenessMixin(BaseModel):
    """The common base `MASTER_ARCHITECTURE.md`'s own note suggested as
    one option ("a common base the per-capability summary models each
    include"). `warnings` is the evidence a caller can act on;
    `evidence_completeness` is the one-glance verdict derived from it by
    the capability's own service-layer code -- this mixin does not
    compute or validate the relationship between the two itself (kept
    simple deliberately: each capability's own existing warning-
    collection logic is trusted internal code, not a boundary that needs
    its own enforcement layer on top)."""

    warnings: list[str] = Field(default_factory=list)
    evidence_completeness: EvidenceCompleteness = EvidenceCompleteness.COMPLETE

"""AI summary: an advisory, non-authoritative explanation of an already-computed
diagnosis -- never a new hypothesis, never a remediation decision.

See ai/summarizer.py for how this is built and RBAC_AND_THREAT_MODEL.md for why
an `AiSummary` has no field that could ever be mistaken for an instruction.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

DISCLAIMER = (
    "AI-generated summary, advisory only. Does not add evidence, change any "
    "diagnosis, or authorize any action -- the diagnostic engine and policy "
    "engine already produced everything this text describes."
)


class AiSummary(BaseModel):
    incident_id: str
    text: str
    model: str
    generated_at: datetime
    disclaimer: str = DISCLAIMER

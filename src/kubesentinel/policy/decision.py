"""The policy engine's output: never a bare boolean.

Every decision carries a human-readable reason, because "the policy engine said no"
is useless in an audit log or an API response without saying *why* -- which check
actually fired.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class PolicyOutcome(str, Enum):
    #: Safe to execute immediately (CONTROLLED_AUTO mode, every check passed, and
    #: this action type is low-risk enough to run unattended).
    ALLOWED = "ALLOWED"
    #: Eligible to be proposed, but a human must approve it before it executes --
    #: either because the mode demands it, the action type is inherently higher-risk
    #: (rollback, scale), or an auto-execution criterion (severity/confidence/
    #: availability impact) wasn't met.
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    #: Must never execute, with or without approval -- mode is OBSERVE, the target
    #: is outside the allowlist, a cooldown is still active, a budget is exhausted,
    #: or the action type doesn't apply to this resource type at all.
    DENIED = "DENIED"


class PolicyDecision(BaseModel):
    outcome: PolicyOutcome
    reason: str

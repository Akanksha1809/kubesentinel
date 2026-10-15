"""Remediation action lifecycle models.

Diagnosis is separate from execution: an Action is only ever created as a *proposal*
from the diagnostic + policy layers. The policy engine (not this module) is the sole
authority on whether an action may run. See kubesentinel.policy.engine.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    RESTART_DEPLOYMENT = "RESTART_DEPLOYMENT"
    ROLLBACK_DEPLOYMENT = "ROLLBACK_DEPLOYMENT"
    SCALE_DEPLOYMENT = "SCALE_DEPLOYMENT"
    RECOMMEND_RESOURCE_ADJUSTMENT = "RECOMMEND_RESOURCE_ADJUSTMENT"
    RECOMMEND_CONFIG_FIX = "RECOMMEND_CONFIG_FIX"


class ActionStatus(str, Enum):
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTED = "EXECUTED"
    VERIFYING = "VERIFYING"
    VERIFIED_RECOVERED = "VERIFIED_RECOVERED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    VERIFICATION_INCONCLUSIVE = "VERIFICATION_INCONCLUSIVE"


class ActionTarget(BaseModel):
    namespace: str
    resource_type: str
    resource_name: str
    resource_uid: str | None = None


class VerificationStatus(str, Enum):
    #: The target is confirmed healthy via a fresh Kubernetes API observation --
    #: not inferred from the action's own "success" result.
    RECOVERED = "RECOVERED"
    #: The target shows a confirmed failure signal (e.g. still ImagePullBackOff)
    #: after the action ran -- the action did not fix the problem.
    FAILED = "FAILED"
    #: Neither confirmed healthy nor confirmed still-failing by the verification
    #: deadline -- e.g. a rollout still in progress with no definitive signal
    #: either way. Never collapsed into FAILED or RECOVERED; the spec is explicit
    #: that an incident must not be marked resolved just because the action's own
    #: API call succeeded, and the same caution applies to an unresolved check.
    INCONCLUSIVE = "INCONCLUSIVE"


class VerificationOutcome(BaseModel):
    checked_at: datetime
    status: VerificationStatus
    reason: str
    details: dict = Field(default_factory=dict)

    @property
    def recovered(self) -> bool:
        return self.status == VerificationStatus.RECOVERED


class RemediationAction(BaseModel):
    action_id: str
    incident_id: str
    action_type: ActionType
    target: ActionTarget
    status: ActionStatus = ActionStatus.PROPOSED
    requires_approval: bool = True
    rationale: str
    # Non-secret parameters only, e.g. {"revision": 3} or {"replicas": 2}.
    parameters: dict = Field(default_factory=dict)

    proposed_at: datetime
    approved_at: datetime | None = None
    approved_by: str | None = None
    executed_at: datetime | None = None
    completed_at: datetime | None = None

    execution_result: dict | None = None
    execution_error: str | None = None
    verification: VerificationOutcome | None = None

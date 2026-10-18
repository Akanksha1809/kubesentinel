"""Orchestrates diagnosis -> policy -> execution for one incident's remediation.

This is the first piece of code in the project that actually calls
`PolicyEngine.evaluate()` with live data and, if the decision is `ALLOWED` or a
human approves a `REQUIRES_APPROVAL` proposal, calls the executor that mutates the
cluster. Everything upstream of this (diagnosis, the policy engine itself) stays
pure and side-effect-free; this module is deliberately where that stops.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from kubesentinel.config import Settings
from kubesentinel.metrics.registry import (
    actions_requiring_approval,
    recovery_duration_seconds,
    remediation_attempts_total,
    remediation_failures_total,
    remediation_success_total,
)
from kubesentinel.models.action import (
    ActionStatus,
    ActionTarget,
    ActionType,
    RemediationAction,
    VerificationStatus,
)
from kubesentinel.models.diagnosis import Confidence
from kubesentinel.models.incident import Incident
from kubesentinel.persistence.interface import IncidentRepository
from kubesentinel.policy.decision import PolicyDecision, PolicyOutcome
from kubesentinel.policy.engine import PolicyEngine, PolicyRequest
from kubesentinel.remediation.executor import RemediationExecutor
from kubesentinel.verification.verifier import RecoveryVerifier

logger = logging.getLogger(__name__)


class ActionNotFoundError(Exception):
    pass


class InvalidActionStateError(Exception):
    """Raised when an operation doesn't match the action's current lifecycle state
    (e.g. approving an action that isn't PENDING_APPROVAL)."""


class RemediationService:
    def __init__(
        self,
        policy_engine: PolicyEngine,
        executor: RemediationExecutor,
        verifier: RecoveryVerifier,
        repo: IncidentRepository,
        settings: Settings,
    ):
        self._policy = policy_engine
        self._executor = executor
        self._verifier = verifier
        self._repo = repo
        self._settings = settings
        # Guards propose()'s read-recent-actions -> decide -> persist sequence.
        # Found necessary live (Milestone 9): `recent_actions` is read fresh
        # from the repository on every call, and the policy decision it feeds
        # is only persisted afterward via `repo.save()` -- with no lock, N
        # concurrent propose() calls on the same incident can all read "0
        # actions so far" before any of them has saved, each independently
        # deciding the per-incident/per-window budget isn't exceeded yet.
        # Confirmed with 10 concurrent callers against a budget of 3: all 10
        # were accepted. A single process-wide lock is sufficient and
        # proportionate for this project's single-process, local-tool design
        # (same reasoning as SQLiteIncidentRepository's own lock) -- it does
        # not protect approve()/execute()/reject() against racing each other
        # on the *same* incident object from two different callers, which
        # remains a known limitation; see TODO.md.
        self._propose_lock = threading.Lock()

    def propose(
        self,
        incident: Incident,
        action_type: ActionType,
        rationale: str,
        parameters: dict | None = None,
        target_desired_replicas: int | None = None,
    ) -> RemediationAction:
        """Build a RemediationAction for `incident`, evaluate it against policy,
        and execute it immediately if the policy allows that without approval.

        `target_desired_replicas` is supplied by the caller, not looked up here --
        this module stays as side-effect-minimal as the policy engine itself about
        *deciding*; it only reaches out to the cluster via the executor, once a
        decision has already been made.
        """
        # Holds `_propose_lock` for the entire read-recent-actions -> decide ->
        # persist sequence -- see the lock's own docstring in __init__ for the
        # concurrency bug this closes.
        with self._propose_lock:
            target = ActionTarget(
                namespace=incident.resource.namespace,
                resource_type=incident.resource.kind,
                resource_name=incident.resource.name,
                resource_uid=incident.resource.uid,
            )
            action = RemediationAction(
                action_id=str(uuid4()),
                incident_id=incident.incident_id,
                action_type=action_type,
                target=target,
                rationale=rationale,
                parameters=parameters or {},
                proposed_at=datetime.now(timezone.utc),
            )

            top = incident.latest_diagnosis.top_hypothesis if incident.latest_diagnosis else None
            now = action.proposed_at
            request = PolicyRequest(
                action_type=action_type,
                target=target,
                incident_id=incident.incident_id,
                incident_severity=incident.severity,
                hypothesis_confidence=top.confidence if top else Confidence.INCONCLUSIVE,
                hypothesis_auto_remediation_permitted=top.auto_remediation_permitted if top else False,
                now=now,
                target_desired_replicas=target_desired_replicas,
                recent_actions=self._repo.list_actions_since(
                    now - timedelta(seconds=self._settings.action_window_seconds)
                ),
            )
            decision = self._policy.evaluate(request)

            remediation_attempts_total.labels(action_type=action_type.value).inc()
            incident.add_audit(
                "remediation_proposed",
                actor="remediation_service",
                action_id=action.action_id,
                action_type=action_type.value,
                policy_outcome=decision.outcome.value,
                policy_reason=decision.reason,
            )

            if decision.outcome == PolicyOutcome.DENIED:
                action.status = ActionStatus.REJECTED
                action.execution_error = f"denied by policy: {decision.reason}"
                remediation_failures_total.labels(action_type=action_type.value, reason="policy_denied").inc()
            elif decision.outcome == PolicyOutcome.REQUIRES_APPROVAL:
                action.status = ActionStatus.PENDING_APPROVAL
                actions_requiring_approval.labels(action_type=action_type.value).inc()
            else:  # ALLOWED
                action.status = ActionStatus.APPROVED
                action.approved_at = now
                action.approved_by = "policy-engine(CONTROLLED_AUTO)"
                self._run(action, incident.category)

            incident.actions.append(action)
            self._repo.save(incident)
            return action

    def dry_run(
        self,
        incident: Incident,
        action_type: ActionType,
        parameters: dict | None = None,
        target_desired_replicas: int | None = None,
    ) -> PolicyDecision:
        """Preview what `propose()` would decide, with zero side effects: no
        RemediationAction is built, persisted, or executed, and no metric is
        incremented. Reads the real current action history for a realistic
        cooldown/budget preview, but writes nothing."""
        target = ActionTarget(
            namespace=incident.resource.namespace,
            resource_type=incident.resource.kind,
            resource_name=incident.resource.name,
            resource_uid=incident.resource.uid,
        )
        top = incident.latest_diagnosis.top_hypothesis if incident.latest_diagnosis else None
        now = datetime.now(timezone.utc)
        request = PolicyRequest(
            action_type=action_type,
            target=target,
            incident_id=incident.incident_id,
            incident_severity=incident.severity,
            hypothesis_confidence=top.confidence if top else Confidence.INCONCLUSIVE,
            hypothesis_auto_remediation_permitted=top.auto_remediation_permitted if top else False,
            now=now,
            target_desired_replicas=target_desired_replicas,
            recent_actions=self._repo.list_actions_since(now - timedelta(seconds=self._settings.action_window_seconds)),
        )
        return self._policy.evaluate(request)

    def approve(self, incident: Incident, action_id: str, approved_by: str) -> RemediationAction:
        """Marks a PENDING_APPROVAL action APPROVED. Deliberately does NOT execute
        it -- that is a separate, explicit `execute()` call, so a human can approve
        now and trigger execution later (or never), matching the spec's three
        distinct operations (plan/approve/execute) rather than collapsing the last
        two into one."""
        action = self._find_action(incident, action_id)
        if action.status != ActionStatus.PENDING_APPROVAL:
            raise InvalidActionStateError(
                f"action {action_id} is {action.status.value}, not PENDING_APPROVAL -- cannot approve"
            )
        action.status = ActionStatus.APPROVED
        action.approved_at = datetime.now(timezone.utc)
        action.approved_by = approved_by
        incident.add_audit("remediation_approved", actor=approved_by, action_id=action_id)
        self._repo.save(incident)
        return action

    def execute(self, incident: Incident, action_id: str) -> RemediationAction:
        """Runs an already-APPROVED action through the executor and verifier.
        A CONTROLLED_AUTO-allowed action never reaches here needing a separate
        call -- `propose()` already ran it inline, since there's no human to wait
        for. This is specifically the second half of the human-approval path."""
        action = self._find_action(incident, action_id)
        if action.status != ActionStatus.APPROVED:
            raise InvalidActionStateError(
                f"action {action_id} is {action.status.value}, not APPROVED -- cannot execute"
            )
        self._run(action, incident.category)
        self._repo.save(incident)
        return action

    def reject(self, incident: Incident, action_id: str, rejected_by: str, reason: str) -> RemediationAction:
        action = self._find_action(incident, action_id)
        if action.status != ActionStatus.PENDING_APPROVAL:
            raise InvalidActionStateError(
                f"action {action_id} is {action.status.value}, not PENDING_APPROVAL -- cannot reject"
            )
        action.status = ActionStatus.REJECTED
        action.execution_error = f"rejected by {rejected_by}: {reason}"
        incident.add_audit("remediation_rejected", actor=rejected_by, action_id=action_id, reason=reason)
        self._repo.save(incident)
        return action

    # -- Standalone actions: no owning Incident at all (docs/
    # RESOURCE_OPTIMIZATION_DESIGN.md Section 14.1) -- e.g. applying a
    # ResourceRecommendation, which usually has no incident. Same
    # propose -> policy -> maybe-execute shape as propose() above, same
    # PolicyEngine/RemediationExecutor/RecoveryVerifier underneath, just
    # persisted independently (save_standalone_action) rather than nested
    # inside an Incident's own .actions list, and with no diagnosis to
    # draw hypothesis_confidence/auto_remediation_permitted from -- both
    # are fixed at their most conservative values (INCONCLUSIVE/False),
    # which already makes CONTROLLED_AUTO's unattended path unreachable
    # for any standalone action, before even considering
    # _ALWAYS_REQUIRES_APPROVAL.

    def propose_standalone(
        self,
        *,
        target: ActionTarget,
        action_type: ActionType,
        rationale: str,
        category: str,
        parameters: dict | None = None,
        target_desired_replicas: int | None = None,
    ) -> RemediationAction:
        with self._propose_lock:
            action = RemediationAction(
                action_id=str(uuid4()),
                incident_id=None,
                action_type=action_type,
                target=target,
                rationale=rationale,
                parameters=parameters or {},
                proposed_at=datetime.now(timezone.utc),
            )
            now = action.proposed_at
            request = PolicyRequest(
                action_type=action_type,
                target=target,
                incident_id=None,
                incident_severity=None,
                hypothesis_confidence=Confidence.INCONCLUSIVE,
                hypothesis_auto_remediation_permitted=False,
                now=now,
                target_desired_replicas=target_desired_replicas,
                recent_actions=self._repo.list_actions_since(
                    now - timedelta(seconds=self._settings.action_window_seconds)
                ),
            )
            decision = self._policy.evaluate(request)
            remediation_attempts_total.labels(action_type=action_type.value).inc()

            if decision.outcome == PolicyOutcome.DENIED:
                action.status = ActionStatus.REJECTED
                action.execution_error = f"denied by policy: {decision.reason}"
                remediation_failures_total.labels(action_type=action_type.value, reason="policy_denied").inc()
            elif decision.outcome == PolicyOutcome.REQUIRES_APPROVAL:
                action.status = ActionStatus.PENDING_APPROVAL
                actions_requiring_approval.labels(action_type=action_type.value).inc()
            else:  # ALLOWED
                action.status = ActionStatus.APPROVED
                action.approved_at = now
                action.approved_by = "policy-engine(CONTROLLED_AUTO)"
                self._run(action, category)

            self._repo.save_standalone_action(action)
            return action

    def dry_run_standalone(
        self,
        *,
        target: ActionTarget,
        action_type: ActionType,
        target_desired_replicas: int | None = None,
    ) -> PolicyDecision:
        """Preview, zero side effects -- mirrors `dry_run()` above."""
        now = datetime.now(timezone.utc)
        request = PolicyRequest(
            action_type=action_type,
            target=target,
            incident_id=None,
            incident_severity=None,
            hypothesis_confidence=Confidence.INCONCLUSIVE,
            hypothesis_auto_remediation_permitted=False,
            now=now,
            target_desired_replicas=target_desired_replicas,
            recent_actions=self._repo.list_actions_since(now - timedelta(seconds=self._settings.action_window_seconds)),
        )
        return self._policy.evaluate(request)

    def approve_standalone(self, action_id: str, approved_by: str) -> RemediationAction:
        action = self._find_standalone_action(action_id)
        if action.status != ActionStatus.PENDING_APPROVAL:
            raise InvalidActionStateError(
                f"action {action_id} is {action.status.value}, not PENDING_APPROVAL -- cannot approve"
            )
        action.status = ActionStatus.APPROVED
        action.approved_at = datetime.now(timezone.utc)
        action.approved_by = approved_by
        self._repo.save_standalone_action(action)
        return action

    def execute_standalone(self, action_id: str, category: str) -> RemediationAction:
        action = self._find_standalone_action(action_id)
        if action.status != ActionStatus.APPROVED:
            raise InvalidActionStateError(
                f"action {action_id} is {action.status.value}, not APPROVED -- cannot execute"
            )
        self._run(action, category)
        self._repo.save_standalone_action(action)
        return action

    def reject_standalone(self, action_id: str, rejected_by: str, reason: str) -> RemediationAction:
        action = self._find_standalone_action(action_id)
        if action.status != ActionStatus.PENDING_APPROVAL:
            raise InvalidActionStateError(
                f"action {action_id} is {action.status.value}, not PENDING_APPROVAL -- cannot reject"
            )
        action.status = ActionStatus.REJECTED
        action.execution_error = f"rejected by {rejected_by}: {reason}"
        self._repo.save_standalone_action(action)
        return action

    def get_standalone_action(self, action_id: str) -> RemediationAction:
        return self._find_standalone_action(action_id)

    def _find_standalone_action(self, action_id: str) -> RemediationAction:
        action = self._repo.get_standalone_action(action_id)
        if action is None:
            raise ActionNotFoundError(f"no standalone action {action_id}")
        return action

    def _run(self, action: RemediationAction, incident_category: str) -> None:
        action.status = ActionStatus.EXECUTING
        self._executor.execute(action)  # mutates action's status/result/error in place

        if action.status != ActionStatus.EXECUTED:
            # The action itself failed to even apply -- nothing to verify. Per the
            # spec: never mark anything resolved on an action that didn't succeed,
            # and stop here rather than inventing a verification result for a
            # mutation that never happened.
            remediation_failures_total.labels(
                action_type=action.action_type.value, reason="execution_failed"
            ).inc()
            return

        action.status = ActionStatus.VERIFYING
        outcome = self._verifier.verify(action)  # blocks up to the verification deadline
        action.verification = outcome

        if outcome.status == VerificationStatus.RECOVERED:
            action.status = ActionStatus.VERIFIED_RECOVERED
            remediation_success_total.labels(action_type=action.action_type.value).inc()
            if action.executed_at is not None:
                elapsed = (outcome.checked_at - action.executed_at).total_seconds()
                recovery_duration_seconds.labels(category=incident_category).observe(elapsed)
        elif outcome.status == VerificationStatus.FAILED:
            action.status = ActionStatus.VERIFICATION_FAILED
            remediation_failures_total.labels(
                action_type=action.action_type.value, reason="verification_failed"
            ).inc()
        else:  # INCONCLUSIVE
            action.status = ActionStatus.VERIFICATION_INCONCLUSIVE
            remediation_failures_total.labels(
                action_type=action.action_type.value, reason="verification_inconclusive"
            ).inc()

    @staticmethod
    def _find_action(incident: Incident, action_id: str) -> RemediationAction:
        for action in incident.actions:
            if action.action_id == action_id:
                return action
        raise ActionNotFoundError(f"no action {action_id} on incident {incident.incident_id}")

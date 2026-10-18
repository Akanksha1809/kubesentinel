"""The policy engine: the sole authority on whether a remediation action may run.

Every check here is deliberately conservative: anything ambiguous or not
explicitly covered falls back to REQUIRES_APPROVAL, never to ALLOWED. The only way
to reach ALLOWED is CONTROLLED_AUTO mode, every DENY-capable check passing, and the
specific action type/severity/confidence combination this module treats as safe to
run unattended (see `_auto_execution_eligible`).

This module never touches the Kubernetes API, the database, or the clock on its
own -- every fact it needs (recent action history, "now") is supplied by the
caller in `PolicyRequest`, exactly like `DiagnosticEngine` only ever sees the
evidence it's handed. That's what makes every branch below directly unit-testable
without mocking a cluster or a repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from kubesentinel.config import PolicyMode, Settings
from kubesentinel.models.action import ActionStatus, ActionTarget, ActionType, RemediationAction
from kubesentinel.models.diagnosis import Confidence
from kubesentinel.models.incident import IncidentSeverity
from kubesentinel.policy.decision import PolicyDecision, PolicyOutcome

# Action types that mutate cluster state and therefore go through the full set of
# checks below. RECOMMEND_* types never touch the cluster -- they only ever
# surface a suggestion for a human to act on -- so they carry no remediation risk
# and are always allowed regardless of mode, allowlist, cooldown, or budget.
_ADVISORY_ACTION_TYPES = {ActionType.RECOMMEND_RESOURCE_ADJUSTMENT, ActionType.RECOMMEND_CONFIG_FIX}

# Action types that only ever make sense against a Deployment. Restarting/rolling
# back/scaling a bare Pod isn't a coherent operation this project defines -- there's
# no controller to recreate it from, which is this project's reading of the spec's
# "whether the resource is managed by a controller" policy criterion.
# ADJUST_RESOURCES (docs/RESOURCE_OPTIMIZATION_DESIGN.md Section 14.2 point 5)
# reuses this exact same reasoning rather than relaxing it: StatefulSet/
# DaemonSet would work the same way in principle, but Job can't be patched
# once running and a standalone Pod has no controller to patch at all --
# scoped to Deployment only, like every action type already here.
_DEPLOYMENT_ONLY_ACTION_TYPES = {
    ActionType.RESTART_DEPLOYMENT,
    ActionType.ROLLBACK_DEPLOYMENT,
    ActionType.SCALE_DEPLOYMENT,
    ActionType.ADJUST_RESOURCES,
}

# Action types the spec treats as higher-impact than a plain restart (rollback
# changes the running code; scaling changes capacity) -- these always require a
# human, even in CONTROLLED_AUTO mode, matching the spec's explicit "approval-gated
# scaling." ADJUST_RESOURCES joins them for the same reason: it changes running
# Pods' resource allocation, a higher blast radius than a plain restart, and this
# project has no correctness evidence yet strong enough to let it run unattended.
_ALWAYS_REQUIRES_APPROVAL = {ActionType.ROLLBACK_DEPLOYMENT, ActionType.SCALE_DEPLOYMENT, ActionType.ADJUST_RESOURCES}


@dataclass
class PolicyRequest:
    action_type: ActionType
    target: ActionTarget
    # None for a standalone action with no owning Incident (see
    # models/action.py::RemediationAction.incident_id) -- when None,
    # _check_per_incident_budget has nothing to check and is skipped; the
    # per-window budget and per-target cooldown both still apply
    # unconditionally, so a standalone action is never *less* bounded, only
    # missing the one check that has no meaning without an incident.
    incident_id: str | None
    # None alongside incident_id -- there is no incident severity to reason
    # about for a standalone action. `_auto_execution_eligible` already
    # requires HIGH+ severity, so None here simply can never qualify for
    # unattended execution, with no special-casing needed.
    incident_severity: IncidentSeverity | None
    hypothesis_confidence: Confidence
    # Whether the diagnostic hypothesis that triggered this proposal believes this
    # type of action can actually help. Every rule through Milestone 2 sets this to
    # False on every hypothesis it produces (CrashLoopBackOff, OOMKilled, and
    # ImagePullBackOff/ErrImagePull are all explicitly "a restart will not fix
    # this") -- so today, correctly, nothing is ever auto-execution-eligible. A
    # human can still approve the same action anyway via APPROVAL_REQUIRED; this
    # flag only gates the *unattended* CONTROLLED_AUTO path, never a human's own
    # judgment call.
    hypothesis_auto_remediation_permitted: bool
    now: datetime
    # Desired replica count of the target Deployment, if known. None means
    # "unknown" and is treated conservatively (as if it could be a singleton) --
    # see `_is_availability_impacting`.
    target_desired_replicas: int | None = None
    # Every RemediationAction previously proposed for this target's resource_uid
    # or incident_id, across all incidents, needed for cooldown and budget checks.
    # The caller (the future remediation engine, or a test) supplies this; the
    # policy engine never queries a repository itself.
    recent_actions: list[RemediationAction] = field(default_factory=list)


class PolicyEngine:
    def __init__(self, settings: Settings):
        self._settings = settings

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        if request.action_type in _ADVISORY_ACTION_TYPES:
            return PolicyDecision(
                outcome=PolicyOutcome.ALLOWED,
                reason="advisory action type -- never mutates cluster state, nothing to gate",
            )

        for check in (
            self._check_mode_not_observe,
            self._check_namespace_allowlist,
            self._check_resource_name_allowlist,
            self._check_resource_type_matches_action,
            self._check_cooldown,
            self._check_per_incident_budget,
            self._check_per_window_budget,
        ):
            decision = check(request)
            if decision is not None:
                return decision

        return self._decide_mode_outcome(request)

    def _check_mode_not_observe(self, request: PolicyRequest) -> PolicyDecision | None:
        if self._settings.policy_mode == PolicyMode.OBSERVE:
            return PolicyDecision(
                outcome=PolicyOutcome.DENIED,
                reason="policy mode is OBSERVE -- remediation is disabled entirely, not even proposed",
            )
        return None

    def _check_namespace_allowlist(self, request: PolicyRequest) -> PolicyDecision | None:
        allowed = self._settings.action_allowed_namespace_list
        if request.target.namespace not in allowed:
            return PolicyDecision(
                outcome=PolicyOutcome.DENIED,
                reason=f"namespace '{request.target.namespace}' is not in the remediation "
                f"namespace allowlist {allowed}",
            )
        return None

    def _check_resource_name_allowlist(self, request: PolicyRequest) -> PolicyDecision | None:
        allowed = self._settings.action_allowed_resource_name_list
        if allowed and request.target.resource_name not in allowed:
            return PolicyDecision(
                outcome=PolicyOutcome.DENIED,
                reason=f"resource '{request.target.resource_name}' is not in the remediation "
                f"resource-name allowlist {allowed}",
            )
        return None

    def _check_resource_type_matches_action(self, request: PolicyRequest) -> PolicyDecision | None:
        if request.action_type in _DEPLOYMENT_ONLY_ACTION_TYPES and request.target.resource_type != "Deployment":
            return PolicyDecision(
                outcome=PolicyOutcome.DENIED,
                reason=f"{request.action_type.value} only applies to Deployment resources, "
                f"got resource_type={request.target.resource_type!r}",
            )
        return None

    def _check_cooldown(self, request: PolicyRequest) -> PolicyDecision | None:
        # REJECTED actions never ran, so they never started a cooldown -- same
        # reasoning as the budget checks below.
        same_target_and_type = [
            a
            for a in request.recent_actions
            if a.action_type == request.action_type
            and a.target.resource_uid == request.target.resource_uid
            and a.status != ActionStatus.REJECTED
        ]
        if not same_target_and_type:
            return None
        last = max(same_target_and_type, key=lambda a: a.proposed_at)
        elapsed = (request.now - last.proposed_at).total_seconds()
        if elapsed < self._settings.action_cooldown_seconds:
            return PolicyDecision(
                outcome=PolicyOutcome.DENIED,
                reason=f"cooldown active: {request.action_type.value} on this target was last proposed "
                f"{elapsed:.0f}s ago, cooldown is {self._settings.action_cooldown_seconds}s",
            )
        return None

    def _check_per_incident_budget(self, request: PolicyRequest) -> PolicyDecision | None:
        if request.incident_id is None:
            # A standalone action has no incident to bound against -- the
            # per-window budget and per-target cooldown checks still apply
            # unconditionally, so this isn't a gap, just a check with
            # nothing to check.
            return None
        count = sum(
            1
            for a in request.recent_actions
            if a.incident_id == request.incident_id and a.status != ActionStatus.REJECTED
        )
        if count >= self._settings.max_actions_per_incident:
            return PolicyDecision(
                outcome=PolicyOutcome.DENIED,
                reason=f"incident {request.incident_id} already has {count} actions, at or above "
                f"the per-incident budget of {self._settings.max_actions_per_incident}",
            )
        return None

    def _check_per_window_budget(self, request: PolicyRequest) -> PolicyDecision | None:
        # REJECTED actions never touched the cluster, so they're excluded from
        # both budgets -- found live: a few harmless, unrelated rejections (e.g.
        # from an active cooldown on a *different* action type) would otherwise
        # permanently exhaust an incident's remediation budget without a single
        # action ever having actually run. EXECUTION_FAILED still counts: it did
        # reach the cluster and is exactly the kind of repeated-attempt this
        # budget exists to bound.
        window_seconds = self._settings.action_window_seconds
        count = sum(
            1
            for a in request.recent_actions
            if a.status != ActionStatus.REJECTED
            and (request.now - a.proposed_at).total_seconds() <= window_seconds
        )
        if count >= self._settings.max_actions_per_window:
            return PolicyDecision(
                outcome=PolicyOutcome.DENIED,
                reason=f"{count} actions proposed in the last {window_seconds}s, at or above the "
                f"per-window budget of {self._settings.max_actions_per_window}",
            )
        return None

    def _decide_mode_outcome(self, request: PolicyRequest) -> PolicyDecision:
        if self._settings.policy_mode == PolicyMode.APPROVAL_REQUIRED:
            return PolicyDecision(
                outcome=PolicyOutcome.REQUIRES_APPROVAL,
                reason="policy mode is APPROVAL_REQUIRED -- every action needs explicit approval",
            )

        # CONTROLLED_AUTO from here on.
        if request.action_type in _ALWAYS_REQUIRES_APPROVAL:
            return PolicyDecision(
                outcome=PolicyOutcome.REQUIRES_APPROVAL,
                reason=f"{request.action_type.value} always requires approval, even in "
                "CONTROLLED_AUTO -- higher-impact than a restart",
            )

        if not self._auto_execution_eligible(request):
            return PolicyDecision(
                outcome=PolicyOutcome.REQUIRES_APPROVAL,
                reason="CONTROLLED_AUTO, but this incident's severity/confidence or the target's "
                "availability impact doesn't meet the bar for unattended execution",
            )

        return PolicyDecision(
            outcome=PolicyOutcome.ALLOWED,
            reason="CONTROLLED_AUTO, all allowlist/cooldown/budget checks passed, the diagnosing "
            "rule itself permits auto-remediation, incident is HIGH+ severity with HIGH "
            "confidence, and the target isn't a single-replica Deployment",
        )

    def _auto_execution_eligible(self, request: PolicyRequest) -> bool:
        if request.action_type != ActionType.RESTART_DEPLOYMENT:
            # Only restart is ever eligible for unattended execution today -- the
            # _ALWAYS_REQUIRES_APPROVAL set already catches rollback/scale, this
            # is just making the restart-only intent explicit rather than implicit.
            return False
        if not request.hypothesis_auto_remediation_permitted:
            # The rule that diagnosed this incident does not itself believe this
            # action will help -- the policy engine defers to that judgment for
            # the unattended path, regardless of how severe or confident the
            # diagnosis otherwise is.
            return False
        if request.incident_severity not in (IncidentSeverity.HIGH, IncidentSeverity.CRITICAL):
            return False
        if request.hypothesis_confidence != Confidence.HIGH:
            return False
        if self._is_availability_impacting(request):
            return False
        return True

    def _is_availability_impacting(self, request: PolicyRequest) -> bool:
        # Unknown replica count is treated conservatively, as if it could be a
        # singleton -- restarting a Deployment's only replica causes a real
        # availability gap, and this project would rather require a human to
        # confirm that's acceptable than guess.
        return request.target_desired_replicas is None or request.target_desired_replicas <= 1

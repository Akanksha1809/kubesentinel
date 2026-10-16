"""Reconciles one `KubeSentinelRemediationPolicy` CR -- see
docs/OPERATOR_DESIGN.md §5 for the exact, numbered steps this
implements. Never decides which `ActionType` to propose for an incident
(docs/OPERATOR_DESIGN.md §2) -- it only re-evaluates `RemediationAction`s
a human already proposed and left `PENDING_APPROVAL`, against the policy
the CR currently declares, calling the exact same
`PolicyEngine.evaluate()`/`RemediationService.approve()`/`.execute()`
every other caller already uses.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from kubernetes.client.rest import ApiException
from pydantic import ValidationError

from kubesentinel.config import Settings
from kubesentinel.models.action import ActionStatus, ActionTarget, RemediationAction
from kubesentinel.models.diagnosis import Confidence
from kubesentinel.models.incident import Incident
from kubesentinel.models.operator import RemediationPolicyRef, RemediationPolicySpec
from kubesentinel.observer.client import ClusterClients
from kubesentinel.operator import GROUP, PLURAL, VERSION
from kubesentinel.persistence.interface import IncidentRepository
from kubesentinel.policy.engine import PolicyEngine, PolicyRequest
from kubesentinel.policy.decision import PolicyOutcome
from kubesentinel.remediation.executor import RemediationExecutor
from kubesentinel.remediation.service import RemediationService
from kubesentinel.verification.verifier import RecoveryVerifier

logger = logging.getLogger(__name__)


class RemediationPolicyController:
    def __init__(self, base_settings: Settings, clients: ClusterClients, incident_repo: IncidentRepository):
        self._base_settings = base_settings
        self._clients = clients
        self._incident_repo = incident_repo

    def reconcile(self, cr: dict) -> None:
        ref = RemediationPolicyRef.from_cr(cr)
        try:
            spec = RemediationPolicySpec.from_cr(cr)
        except ValidationError as exc:
            # The CRD's own OpenAPI schema already rejects most malformed
            # specs at admission time -- this is a second, defensive layer
            # (e.g. a value the schema allows but this model is stricter
            # about), reported on the CR's own status rather than crashing
            # the whole reconcile loop over one bad CR.
            logger.warning("invalid RemediationPolicySpec", extra={"policy_name": ref.name, "error": str(exc)})
            self._patch_status(ref, pending_seen=0, auto_approved=0, message=f"invalid spec: {exc}")
            return

        scoped_settings = self._base_settings.model_copy(
            update={
                "policy_mode": spec.mode,
                "action_cooldown_seconds": spec.cooldown_seconds,
                "max_actions_per_incident": spec.max_actions_per_incident,
                "max_actions_per_window": spec.max_actions_per_window,
                "action_window_seconds": spec.window_seconds,
                "action_allowed_resource_names": ",".join(spec.allowed_resource_names),
            }
        )
        policy_engine = PolicyEngine(scoped_settings)
        executor = RemediationExecutor(
            self._clients.apps, timeout_seconds=scoped_settings.action_execution_timeout_seconds
        )
        verifier = RecoveryVerifier(
            self._clients.apps,
            deadline_seconds=scoped_settings.verification_deadline_seconds,
            poll_interval_seconds=scoped_settings.verification_poll_interval_seconds,
        )
        remediation_service = RemediationService(
            policy_engine, executor, verifier, self._incident_repo, scoped_settings
        )

        pending_seen = 0
        auto_approved = 0
        for incident in self._incident_repo.list(namespace=ref.namespace):
            for action in incident.actions:
                if action.status != ActionStatus.PENDING_APPROVAL:
                    continue
                pending_seen += 1
                if self._maybe_auto_approve(incident, action, scoped_settings, policy_engine, remediation_service):
                    auto_approved += 1

        self._patch_status(ref, pending_seen, auto_approved, message="reconciled")

    def _maybe_auto_approve(
        self,
        incident: Incident,
        action: RemediationAction,
        scoped_settings: Settings,
        policy_engine: PolicyEngine,
        remediation_service: RemediationService,
    ) -> bool:
        now = datetime.now(timezone.utc)
        top = incident.latest_diagnosis.top_hypothesis if incident.latest_diagnosis else None
        # `action` itself -- not yet approved, still sitting PENDING_APPROVAL
        # -- is already persisted, so list_actions_since() would otherwise
        # include it as its own "recent action" and deny on a self-triggered
        # cooldown (found live writing this test: a 0-second-old cooldown
        # against itself, every single time). `propose()` never hits this,
        # since it builds `recent_actions` *before* the new action is ever
        # appended/saved -- re-evaluating an already-persisted pending
        # action needs this exclusion explicitly.
        recent_actions = [
            a
            for a in self._incident_repo.list_actions_since(
                now - timedelta(seconds=scoped_settings.action_window_seconds)
            )
            if a.action_id != action.action_id
        ]
        request = PolicyRequest(
            action_type=action.action_type,
            target=action.target,
            incident_id=incident.incident_id,
            incident_severity=incident.severity,
            hypothesis_confidence=top.confidence if top else Confidence.INCONCLUSIVE,
            hypothesis_auto_remediation_permitted=top.auto_remediation_permitted if top else False,
            now=now,
            target_desired_replicas=self._current_desired_replicas(action.target),
            recent_actions=recent_actions,
        )
        decision = policy_engine.evaluate(request)
        if decision.outcome != PolicyOutcome.ALLOWED:
            return False

        logger.info(
            "operator auto-approving pending action",
            extra={"incident_id": incident.incident_id, "action_id": action.action_id, "reason": decision.reason},
        )
        remediation_service.approve(incident, action.action_id, approved_by="kubesentinel-operator")
        remediation_service.execute(incident, action.action_id)
        return True

    def _current_desired_replicas(self, target: ActionTarget) -> int | None:
        # Never trust a value the operator might have cached from an
        # earlier reconcile -- read the Deployment's current spec fresh,
        # the same "read live, don't assume" discipline every other
        # capability in this project already applies.
        if target.resource_type != "Deployment":
            return None
        try:
            deployment = self._clients.apps.read_namespaced_deployment(
                name=target.resource_name, namespace=target.namespace
            )
            return deployment.spec.replicas
        except ApiException:
            return None

    def _patch_status(self, ref: RemediationPolicyRef, pending_seen: int, auto_approved: int, message: str) -> None:
        body = {
            "status": {
                "observedGeneration": ref.generation,
                "lastReconcileTime": datetime.now(timezone.utc).isoformat(),
                "pendingActionsSeen": pending_seen,
                "actionsAutoApproved": auto_approved,
                "message": message,
            }
        }
        try:
            self._clients.custom_objects.patch_namespaced_custom_object_status(
                group=GROUP, version=VERSION, namespace=ref.namespace, plural=PLURAL, name=ref.name, body=body
            )
        except ApiException:
            logger.exception("failed to patch RemediationPolicy status", extra={"policy_name": ref.name})

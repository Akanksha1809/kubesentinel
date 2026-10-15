"""Recovery verification: confirms whether a remediation action actually fixed
the problem, via fresh Kubernetes API observations -- never by trusting that the
action's own API call returned success.

Reuses `deployment_evidence`/`is_confirmed_healthy` from Milestones 1-2 for the
RECOVERED side (rather than inventing a second definition of "is this Deployment
healthy"), but deliberately does NOT reuse `has_failure_signal` for the FAILED
side -- see `_deployment_definitively_stuck`'s docstring for why that check,
correct for passive incident detection, is too eager for a verifier that is, by
definition, watching a transition it just triggered.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from kubernetes import client
from kubernetes.client.rest import ApiException

from kubesentinel.incidents.health import is_confirmed_healthy
from kubesentinel.models.action import ActionType, RemediationAction, VerificationOutcome, VerificationStatus
from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.observer.inspector import deployment_evidence

logger = logging.getLogger(__name__)

_ADVISORY_ACTION_TYPES = {ActionType.RECOMMEND_RESOURCE_ADJUSTMENT, ActionType.RECOMMEND_CONFIG_FIX}

# RESTART/ROLLBACK both change spec.template, which starts a new rollout to a new
# ReplicaSet. During that rollout, Kubernetes deliberately keeps old Pods serving
# until new ones are ready -- so the Deployment's aggregate ready/available counts
# can look fully satisfied (via the *old* ReplicaSet) well before the new one has
# actually come up. Confirmed live: checking only `is_confirmed_healthy` reported
# RECOVERED within 0.1s of a restart, while `kubectl get pods` still showed the
# new Pod in ContainerCreating and both old Pods still Running. For these two
# action types specifically, "recovered" has to mean the rollout itself
# completed -- updatedReplicas caught up to desired too -- the same thing
# `kubectl rollout status` waits for. SCALE_DEPLOYMENT doesn't start a new
# rollout, so the simpler aggregate check is the correct one for it.
_ROLLOUT_AWARE_ACTION_TYPES = {ActionType.RESTART_DEPLOYMENT, ActionType.ROLLBACK_DEPLOYMENT}


def _rollout_complete(evidence: list[Evidence]) -> bool:
    snapshots = [e for e in evidence if e.kind == EvidenceKind.DEPLOYMENT_STATUS and e.source_field == "status.replicas"]
    if not snapshots:
        return False
    d = snapshots[-1].data
    return (
        d["ready_replicas"] >= d["desired_replicas"]
        and d["available_replicas"] >= d["desired_replicas"]
        and d["updated_replicas"] >= d["desired_replicas"]
    )


def _deployment_definitively_stuck(evidence: list[Evidence]) -> bool:
    """A verifier-specific, *stricter* failure check than `has_failure_signal`.

    Found live: a routine SCALE_DEPLOYMENT 2->3 on a perfectly healthy Deployment
    was reported `FAILED` because, for one poll, Kubernetes reported
    `Available=False (MinimumReplicasUnavailable)` -- entirely normal while the
    one new replica hadn't started yet. `has_failure_signal` is the right check
    for the *passive* incident manager (where Available=False, observed with no
    context, is a reasonable thing to open an incident about) -- but the
    *active* verifier is, by definition, watching a Deployment through a
    transition it just triggered, where a brief dip in availability is often
    exactly what recovering looks like, not evidence that it failed.
    `ProgressDeadlineExceeded`, by contrast, is only ever set after the
    controller has waited out its own deadline and given up -- that is a
    legitimate, durable "this won't resolve on its own" signal, so it's the
    only one this stricter check still trusts.
    """
    for e in evidence:
        if (
            e.kind == EvidenceKind.DEPLOYMENT_STATUS
            and e.data.get("type") == "Progressing"
            and e.data.get("status") == "False"
            and e.data.get("reason") == "ProgressDeadlineExceeded"
        ):
            return True
    return False


class RecoveryVerifier:
    def __init__(self, apps_api: client.AppsV1Api, deadline_seconds: float = 60.0, poll_interval_seconds: float = 2.0):
        self._apps = apps_api
        self._deadline = deadline_seconds
        self._poll_interval = poll_interval_seconds

    def verify(self, action: RemediationAction) -> VerificationOutcome:
        """Never raises -- any unexpected error becomes an INCONCLUSIVE outcome
        with the error recorded in `details`, exactly like the executor never
        raises an unhandled exception to its own caller."""
        if action.action_type in _ADVISORY_ACTION_TYPES:
            return VerificationOutcome(
                checked_at=datetime.now(timezone.utc),
                status=VerificationStatus.RECOVERED,
                reason="advisory action -- never mutated the cluster, nothing to verify",
            )

        deadline_at = time.monotonic() + self._deadline
        attempts = 0
        last_evidence_summary: list[str] = []

        while True:
            attempts += 1
            try:
                deployment = self._apps.read_namespaced_deployment(
                    name=action.target.resource_name, namespace=action.target.namespace
                )
            except ApiException as exc:
                if exc.status == 404:
                    return VerificationOutcome(
                        checked_at=datetime.now(timezone.utc),
                        status=VerificationStatus.FAILED,
                        reason="target Deployment no longer exists",
                        details={"attempts": attempts},
                    )
                return self._inconclusive_from_error(exc, attempts)
            except Exception as exc:  # noqa: BLE001 - never raise out of verify()
                return self._inconclusive_from_error(exc, attempts)

            evidence = deployment_evidence(deployment)
            last_evidence_summary = [e.summary for e in evidence]

            recovered = (
                _rollout_complete(evidence)
                if action.action_type in _ROLLOUT_AWARE_ACTION_TYPES
                else is_confirmed_healthy(evidence)
            )
            if recovered:
                return VerificationOutcome(
                    checked_at=datetime.now(timezone.utc),
                    status=VerificationStatus.RECOVERED,
                    reason="Deployment reports desired replicas ready and available"
                    + (
                        ", and the new rollout has fully replaced the old ReplicaSet"
                        if action.action_type in _ROLLOUT_AWARE_ACTION_TYPES
                        else ""
                    ),
                    details={"attempts": attempts, "evidence": last_evidence_summary},
                )

            if _deployment_definitively_stuck(evidence):
                return VerificationOutcome(
                    checked_at=datetime.now(timezone.utc),
                    status=VerificationStatus.FAILED,
                    reason="Deployment still shows a confirmed failure signal after the action ran",
                    details={"attempts": attempts, "evidence": last_evidence_summary},
                )

            if time.monotonic() >= deadline_at:
                return VerificationOutcome(
                    checked_at=datetime.now(timezone.utc),
                    status=VerificationStatus.INCONCLUSIVE,
                    reason=f"verification deadline ({self._deadline}s) exceeded with neither confirmed "
                    "recovery nor a confirmed failure signal -- e.g. a rollout still settling",
                    details={"attempts": attempts, "evidence": last_evidence_summary},
                )

            time.sleep(self._poll_interval)

    @staticmethod
    def _inconclusive_from_error(exc: Exception, attempts: int) -> VerificationOutcome:
        logger.warning("verification poll failed, treating as inconclusive", exc_info=exc)
        return VerificationOutcome(
            checked_at=datetime.now(timezone.utc),
            status=VerificationStatus.INCONCLUSIVE,
            reason=f"could not confirm either way: {type(exc).__name__}: {exc}",
            details={"attempts": attempts},
        )

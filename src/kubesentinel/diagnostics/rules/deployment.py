"""Deployment-level rules: rollout failures and unsatisfied replica counts.

Unlike the Pod-level rules, these consume DEPLOYMENT_STATUS evidence and produce
hypotheses for a Deployment-kind incident, not a Pod-kind one -- IncidentManager
dedups by resource UID regardless of kind, so this works without any change to the
manager itself (see ARCHITECTURE.md).
"""

from __future__ import annotations

from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.diagnostics.rules.base import DiagnosticRule

# How long a ready-replica deficit must persist across repeated observations before
# this rule treats it as more than "a normal rollout still catching up." Chosen to be
# comfortably longer than a healthy Pod's typical startup time in the lab scenarios.
_MIN_SUSTAINED_DEFICIT_SECONDS = 30.0


class RolloutFailedRule(DiagnosticRule):
    name = "rollout_failed"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        for ev in _conditions(evidence, "Progressing"):
            if ev.data.get("status") != "False" or ev.data.get("reason") != "ProgressDeadlineExceeded":
                continue
            message = ev.data.get("message") or ""
            hypotheses.append(
                Hypothesis(
                    rule_name=self.name,
                    reason_code=ReasonCode.ROLLOUT_FAILED,
                    confidence=Confidence.HIGH,
                    explanation=(
                        f"The Deployment controller itself reports the rollout failed: {message or 'no message'}. "
                        "This is set only after progressDeadlineSeconds elapses with no progress -- "
                        "not a guess based on replica counts alone."
                    ),
                    supporting_evidence=[ev.id],
                    missing_evidence=[],
                    recommended_next_steps=[
                        "Check the new ReplicaSet's Pods directly for why they aren't becoming ready "
                        "(image pull, crash, readiness probe, resource pressure).",
                        "Consider a rollback to the previous revision if the new one is confirmed broken.",
                    ],
                    auto_remediation_permitted=False,
                )
            )
        return hypotheses


class UnsatisfiedReplicasRule(DiagnosticRule):
    name = "unsatisfied_replicas"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        snapshots = [
            e for e in evidence if e.kind == EvidenceKind.DEPLOYMENT_STATUS and e.source_field == "status.replicas"
        ]
        if not snapshots:
            return []

        latest = snapshots[-1]
        desired = latest.data["desired_replicas"]
        ready = latest.data["ready_replicas"]
        if ready >= desired:
            return []

        progressing = _conditions(evidence, "Progressing")
        latest_progressing = progressing[-1] if progressing else None
        unavailable = [e for e in _conditions(evidence, "Available") if e.data.get("status") == "False"]
        latest_unavailable = unavailable[-1] if unavailable else None

        if latest_progressing is not None and latest_progressing.data.get("reason") == "NewReplicaSetAvailable":
            confidence = Confidence.HIGH
            explanation = (
                f"{ready}/{desired} replicas ready, but the controller itself considers this rollout "
                "already complete (condition Progressing reason=NewReplicaSetAvailable). A deficit "
                "after the controller says it's done means Pods are failing post-rollout, not that "
                "a rollout is still catching up."
            )
            supporting = [latest.id, latest_progressing.id]
            missing: list[str] = []
        elif latest_unavailable is not None:
            confidence = Confidence.HIGH
            explanation = (
                f"{ready}/{desired} replicas ready, and the Deployment's own Available condition is "
                f"False: {latest_unavailable.data.get('message') or 'no message'}. This is the "
                "controller's own confirmation that minimum availability isn't currently met."
            )
            supporting = [latest.id, latest_unavailable.id]
            missing = []
        else:
            deficits = [s for s in snapshots if s.data["ready_replicas"] < s.data["desired_replicas"]]
            span = (
                (deficits[-1].observed_at - deficits[0].observed_at).total_seconds()
                if len(deficits) > 1
                else 0.0
            )
            if span >= _MIN_SUSTAINED_DEFICIT_SECONDS:
                confidence = Confidence.HIGH
                explanation = (
                    f"{ready}/{desired} replicas ready, sustained for {span:.0f}s across "
                    f"{len(deficits)} observations -- long enough that this is unlikely to just be "
                    "a normal rollout still in progress."
                )
            else:
                confidence = Confidence.MEDIUM
                explanation = (
                    f"{ready}/{desired} replicas ready. This could be a normal rollout still "
                    "catching up -- only one (or recent) observation(s) of the deficit so far."
                )
            supporting = [latest.id]
            missing = ["more observations over time to rule out a normal, still-in-progress rollout"]

        return [
            Hypothesis(
                rule_name=self.name,
                reason_code=ReasonCode.UNSATISFIED_REPLICAS,
                confidence=confidence,
                explanation=explanation,
                supporting_evidence=supporting,
                missing_evidence=missing,
                recommended_next_steps=[
                    "Check the Deployment's Pods directly for why they aren't ready -- this rule "
                    "only sees the aggregate replica counts, not the per-Pod cause.",
                ],
                auto_remediation_permitted=False,
            )
        ]


def _conditions(evidence: list[Evidence], condition_type: str) -> list[Evidence]:
    return [
        e
        for e in evidence
        if e.kind == EvidenceKind.DEPLOYMENT_STATUS and e.data.get("type") == condition_type
    ]

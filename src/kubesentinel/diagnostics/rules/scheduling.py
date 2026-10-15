"""Pending pods that the scheduler couldn't place: insufficient resources, taints,
affinity/node-selector mismatches.

All of this is reported by the scheduler itself on the Pod's own `PodScheduled`
condition (`status=False`, `reason=Unschedulable`) -- no Node or Event watch is
needed, because the scheduler writes its explanation directly into the message, e.g.
"0/1 nodes are available: 1 Insufficient cpu, 1 Insufficient memory." or
"0/1 nodes are available: 1 node(s) had untolerated taint ...". The *fact* that the
Pod is unschedulable is always HIGH confidence (a condition the scheduler itself set).
Which specific reason code it maps to is a classification of that same message's
text -- still HIGH confidence when the message clearly names one cause, MEDIUM when it
doesn't match a known pattern and the classification is a fallback guess.
"""

from __future__ import annotations

from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.diagnostics.rules.base import DiagnosticRule


class PendingSchedulingRule(DiagnosticRule):
    name = "pending_scheduling"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        unschedulable = [
            e
            for e in evidence
            if e.kind == EvidenceKind.POD_CONDITION
            and e.data.get("type") == "PodScheduled"
            and e.data.get("status") == "False"
            and e.data.get("reason") == "Unschedulable"
        ]

        for ev in unschedulable:
            message = ev.data.get("message") or ""
            reason_code, confidence, cause = _classify(message)

            hypotheses.append(
                Hypothesis(
                    rule_name=self.name,
                    reason_code=reason_code,
                    confidence=confidence,
                    explanation=(
                        f"The scheduler reports this Pod is unschedulable: {message or 'no message'}. "
                        f"{cause}"
                    ),
                    supporting_evidence=[ev.id],
                    missing_evidence=[] if confidence == Confidence.HIGH else [
                        "a scheduler message matching a known resource/taint/affinity pattern"
                    ],
                    recommended_next_steps=_next_steps(reason_code),
                    auto_remediation_permitted=False,
                )
            )

        return hypotheses


def _classify(message: str) -> tuple[ReasonCode, Confidence, str]:
    lowered = message.lower()
    if "insufficient" in lowered:
        return (
            ReasonCode.PENDING_INSUFFICIENT_RESOURCES,
            Confidence.HIGH,
            "No node currently has enough free CPU/memory/other resources to satisfy this "
            "Pod's requests.",
        )
    if "taint" in lowered:
        return (
            ReasonCode.PENDING_SCHEDULING_CONSTRAINT,
            Confidence.HIGH,
            "No node's taints are tolerated by this Pod's tolerations.",
        )
    if "node affinity" in lowered or "node selector" in lowered or "didn't match" in lowered:
        return (
            ReasonCode.PENDING_SCHEDULING_CONSTRAINT,
            Confidence.HIGH,
            "No node matches this Pod's nodeSelector/nodeAffinity requirements.",
        )
    return (
        ReasonCode.PENDING_SCHEDULING_CONSTRAINT,
        Confidence.MEDIUM,
        "The message doesn't match a known resource/taint/affinity pattern; treating this as "
        "a scheduling constraint is a fallback classification, not a confirmed cause.",
    )


def _next_steps(reason_code: ReasonCode) -> list[str]:
    if reason_code == ReasonCode.PENDING_INSUFFICIENT_RESOURCES:
        return [
            "Check node capacity/allocatable resources vs. this Pod's requests.",
            "Lower the Pod's resource requests, or add node capacity, if the request is "
            "genuinely too high for the cluster.",
        ]
    return [
        "Check node taints against this Pod's tolerations, and any nodeSelector/"
        "nodeAffinity rules on the Pod spec.",
    ]

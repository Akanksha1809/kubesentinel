"""In-Pod volume mount failures.

A different failure mode from `pvc.py`'s own binding failures: a PVC can be
genuinely `Bound` (binding succeeded) while the kubelet still cannot mount the
underlying volume on the Node the Pod landed on -- a wrong `fsType`, an
unreachable backing store (e.g. an NFS server down), a multi-attach conflict
(a RWO volume already attached elsewhere), or a permissions mismatch. None of
these show up as a container waiting/terminated reason (the container never
even starts), so the kubelet's `FailedMount` Warning Event on the Pod is the
only evidence this failure mode produces at all -- confirmed by this rule's
own module, `event_evidence()`, already extracting Pod-involved Events
generically (see observer/inspector.py).
"""

from __future__ import annotations

from kubesentinel.diagnostics.rules.base import DiagnosticRule
from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence, EvidenceKind


class FailedMountRule(DiagnosticRule):
    name = "failed_mount"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        for ev in evidence:
            if ev.kind != EvidenceKind.EVENT or ev.data.get("reason") != "FailedMount":
                continue
            message = ev.data.get("message") or ""
            hypotheses.append(
                Hypothesis(
                    rule_name=self.name,
                    reason_code=ReasonCode.FAILED_MOUNT,
                    confidence=Confidence.HIGH,
                    explanation=f"Pod failed to mount a volume (FailedMount): {message or 'no message'}.",
                    supporting_evidence=[ev.id],
                    missing_evidence=[],
                    recommended_next_steps=[
                        "Check that the volume's PVC is Bound, the backing storage is reachable from "
                        "this Node (e.g. the NFS server, CSI driver, or cloud disk), the volume isn't "
                        "already attached elsewhere (RWO multi-attach conflicts), and that any "
                        "fsType/permissions match what the container expects.",
                    ],
                    auto_remediation_permitted=False,
                )
            )
        return hypotheses

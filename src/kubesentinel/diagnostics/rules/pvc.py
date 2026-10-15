"""PersistentVolumeClaim binding failures.

Deliberately does NOT treat `status.phase == "Pending"` alone as a failure signal.
Confirmed against this project's own kind cluster: the default `local-path` storage
class uses `volumeBindingMode: WaitForFirstConsumer`, under which a PVC is
*correctly* `Pending` until a Pod that references it is scheduled -- that is normal,
expected behavior, not a problem. Treating bare `Pending` as broken would fabricate a
diagnosis the evidence doesn't actually support.

The only evidence this rule trusts is the specific Warning Events Kubernetes emits
when binding/provisioning genuinely fails: `ProvisioningFailed` (the dynamic
provisioner itself failed) and `FailedBinding` (no suitable PersistentVolume could be
found/bound). Both require the PVC and Event watchers to be wired together --
`status.phase` alone, without one of these Events, stays INCONCLUSIVE.
"""

from __future__ import annotations

from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.diagnostics.rules.base import DiagnosticRule

_FAILURE_EVENT_REASONS = {"ProvisioningFailed", "FailedBinding"}


class PvcBindingFailureRule(DiagnosticRule):
    name = "pvc_binding_failure"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        for ev in evidence:
            if ev.kind != EvidenceKind.EVENT or ev.data.get("reason") not in _FAILURE_EVENT_REASONS:
                continue
            message = ev.data.get("message") or ""
            hypotheses.append(
                Hypothesis(
                    rule_name=self.name,
                    reason_code=ReasonCode.PVC_BINDING_FAILURE,
                    confidence=Confidence.HIGH,
                    explanation=(
                        f"PersistentVolumeClaim binding/provisioning failed "
                        f"({ev.data.get('reason')}): {message or 'no message'}."
                    ),
                    supporting_evidence=[ev.id],
                    missing_evidence=[],
                    recommended_next_steps=[
                        "Check that the referenced StorageClass exists and its provisioner is "
                        "running (e.g. local-path-provisioner), and that requested capacity/access "
                        "modes are satisfiable.",
                    ],
                    auto_remediation_permitted=False,
                )
            )
        return hypotheses

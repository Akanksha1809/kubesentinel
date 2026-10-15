"""ImagePullBackOff / ErrImagePull: the kubelet could not pull the container image.

Both are reported via containerStatuses[].state.waiting.reason. ErrImagePull is the
immediate failure; ImagePullBackOff is the kubelet backing off retries after repeated
ErrImagePull. Either way the pod cannot start until the image reference is fixed or
the registry/auth problem is resolved -- restarting the Pod will not help.
"""

from __future__ import annotations

from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.diagnostics.rules.base import DiagnosticRule

_REASON_MAP = {
    "ImagePullBackOff": ReasonCode.IMAGE_PULL_BACKOFF,
    "ErrImagePull": ReasonCode.ERR_IMAGE_PULL,
}


class ImagePullRule(DiagnosticRule):
    name = "image_pull_failure"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        waiting_states = [
            e
            for e in evidence
            if e.kind == EvidenceKind.CONTAINER_STATE and e.data.get("waiting_reason") in _REASON_MAP
        ]

        for ev in waiting_states:
            reason = ev.data["waiting_reason"]
            container = ev.data.get("container")
            message = ev.data.get("waiting_message") or ""

            likely_cause = _classify_message(message)

            hypotheses.append(
                Hypothesis(
                    rule_name=self.name,
                    reason_code=_REASON_MAP[reason],
                    confidence=Confidence.HIGH,
                    explanation=(
                        f"Container '{container}' is waiting with reason {reason}: {message or 'no message'}. "
                        f"{likely_cause}"
                    ),
                    supporting_evidence=[ev.id],
                    missing_evidence=[],
                    recommended_next_steps=[
                        "Verify the image reference (repository, tag, digest) is correct and reachable.",
                        "Check imagePullSecrets if the registry requires authentication.",
                    ],
                    auto_remediation_permitted=False,
                )
            )

        return hypotheses


def _classify_message(message: str) -> str:
    lowered = message.lower()
    if "not found" in lowered or "does not exist" in lowered or "manifest unknown" in lowered:
        return "The image reference likely does not exist in the registry (typo or deleted tag)."
    if "unauthorized" in lowered or "authentication required" in lowered or "denied" in lowered:
        return "The pull likely failed due to missing or invalid registry credentials."
    if "timeout" in lowered or "no such host" in lowered or "connection refused" in lowered:
        return "The pull likely failed due to a network or registry connectivity problem."
    return "Insufficient message detail to classify the exact cause further."

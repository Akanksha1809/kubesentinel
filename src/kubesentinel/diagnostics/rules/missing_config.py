"""Missing ConfigMap/Secret reference: a container can't start because something it
references (an env var's valueFrom, a volume's configMap/secret) doesn't exist.

Kubernetes reports this directly via containerStatuses[].state.waiting.reason in
{"CreateContainerConfigError", "CreateContainerError"} -- a kubelet-reported fact, not
an inference. The waiting message names the missing ConfigMap/Secret/key directly
(e.g. "configmap \"app-config\" not found" or "couldn't find key API_KEY in Secret
default/app-secrets"), which this rule surfaces verbatim rather than re-parsing it
into a guess.
"""

from __future__ import annotations

from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.diagnostics.rules.base import DiagnosticRule

_BAD_WAITING_REASONS = {"CreateContainerConfigError", "CreateContainerError"}


class MissingConfigReferenceRule(DiagnosticRule):
    name = "missing_config_reference"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        waiting_states = [
            e
            for e in evidence
            if e.kind == EvidenceKind.CONTAINER_STATE and e.data.get("waiting_reason") in _BAD_WAITING_REASONS
        ]

        for ev in waiting_states:
            container = ev.data.get("container")
            message = ev.data.get("waiting_message") or ""

            hypotheses.append(
                Hypothesis(
                    rule_name=self.name,
                    reason_code=ReasonCode.MISSING_CONFIG_REFERENCE,
                    confidence=Confidence.HIGH,
                    explanation=(
                        f"Container '{container}' cannot start: {message or 'no message available'}. "
                        "This reason is only set when the kubelet fails to resolve a referenced "
                        "ConfigMap, Secret, or a specific key within one -- the message above names "
                        "exactly what's missing."
                    ),
                    supporting_evidence=[ev.id],
                    missing_evidence=[],
                    recommended_next_steps=[
                        "Check that the referenced ConfigMap/Secret exists in this namespace and "
                        "contains the expected key.",
                        "Verify the Pod spec's envFrom/valueFrom/volume references match the "
                        "ConfigMap/Secret's actual name and keys.",
                    ],
                    auto_remediation_permitted=False,
                )
            )

        return hypotheses

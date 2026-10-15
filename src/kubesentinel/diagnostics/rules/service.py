"""Service selector / EndpointSlice mismatch.

Deliberately narrow: this only fires when a Service's EndpointSlice(s) show *zero*
endpoints at all, not merely zero *ready* endpoints. Zero endpoints means no Pod in
the cluster currently matches the Service's selector -- a real, confirmed fact.
Endpoints present but not-ready is a different situation entirely (matching Pods
exist but aren't passing readiness yet), which is a Pod-level readiness problem
already covered by ReadinessProbeFailingRule, not a Service misconfiguration --
firing this rule in that case would misdiagnose the actual cause.

Even "zero endpoints" can't be called a selector *typo* with full confidence from
this evidence alone (it's equally consistent with "no Pods exist for this Service
yet," e.g. the Deployment hasn't been created, or was scaled to zero) -- confidence
is capped at MEDIUM and the explanation says so explicitly.
"""

from __future__ import annotations

from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.diagnostics.rules.base import DiagnosticRule


class ServiceSelectorMismatchRule(DiagnosticRule):
    name = "service_selector_mismatch"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        snapshots = [e for e in evidence if e.kind == EvidenceKind.ENDPOINTSLICE_STATE]
        if not snapshots:
            return []

        latest = snapshots[-1]
        if latest.data["total_endpoints"] > 0:
            return []

        return [
            Hypothesis(
                rule_name=self.name,
                reason_code=ReasonCode.SERVICE_SELECTOR_MISMATCH,
                confidence=Confidence.MEDIUM,
                explanation=(
                    "This Service's EndpointSlice has zero endpoints -- no Pod currently "
                    "matches its selector. This could mean the selector has a typo/mismatch "
                    "against the Pods it's meant to target, or simply that no matching Pods "
                    "exist yet (e.g. the Deployment hasn't been created, or was scaled to zero)."
                ),
                supporting_evidence=[latest.id],
                missing_evidence=[
                    "confirmation of whether any Pods in this namespace carry labels close to "
                    "this Service's selector"
                ],
                recommended_next_steps=[
                    "Compare this Service's spec.selector against the labels on the Pods it's "
                    "meant to target.",
                    "Check whether the target Deployment/Pods actually exist and are scheduled "
                    "in this namespace.",
                ],
                auto_remediation_permitted=False,
            )
        ]

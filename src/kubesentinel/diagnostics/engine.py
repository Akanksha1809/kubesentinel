"""Runs all registered diagnostic rules against an incident's evidence and ranks results."""

from __future__ import annotations

from datetime import datetime, timezone

from kubesentinel.diagnostics.rules.base import DiagnosticRule
from kubesentinel.diagnostics.rules.crash_loop import CrashLoopBackOffRule
from kubesentinel.diagnostics.rules.deployment import RolloutFailedRule, UnsatisfiedReplicasRule
from kubesentinel.diagnostics.rules.image_pull import ImagePullRule
from kubesentinel.diagnostics.rules.missing_config import MissingConfigReferenceRule
from kubesentinel.diagnostics.rules.node import NodeConditionRule
from kubesentinel.diagnostics.rules.oomkilled import OOMKilledRule
from kubesentinel.diagnostics.rules.probes import LivenessProbeFailingRule, ReadinessProbeFailingRule
from kubesentinel.diagnostics.rules.pvc import PvcBindingFailureRule
from kubesentinel.diagnostics.rules.scheduling import PendingSchedulingRule
from kubesentinel.diagnostics.rules.service import ServiceSelectorMismatchRule
from kubesentinel.metrics.registry import diagnostic_duration_seconds, diagnostics_total
from kubesentinel.models.diagnosis import Confidence, DiagnosisResult
from kubesentinel.models.evidence import Evidence

_CONFIDENCE_ORDER = {
    Confidence.HIGH: 0,
    Confidence.MEDIUM: 1,
    Confidence.LOW: 2,
    Confidence.INCONCLUSIVE: 3,
}

# Order matters as a tiebreak: more specific root-cause rules (OOM) are listed before
# the symptom-level rule (CrashLoopBackOff) so that equal-confidence hypotheses rank
# the more specific one first.
DEFAULT_RULES: list[DiagnosticRule] = [
    OOMKilledRule(),
    ImagePullRule(),
    MissingConfigReferenceRule(),
    PendingSchedulingRule(),
    LivenessProbeFailingRule(),
    ReadinessProbeFailingRule(),
    CrashLoopBackOffRule(),
    RolloutFailedRule(),
    UnsatisfiedReplicasRule(),
    PvcBindingFailureRule(),
    NodeConditionRule(),
    ServiceSelectorMismatchRule(),
]


class DiagnosticEngine:
    def __init__(self, rules: list[DiagnosticRule] | None = None):
        self._rules = rules if rules is not None else DEFAULT_RULES

    def diagnose(self, evidence: list[Evidence]) -> DiagnosisResult:
        with diagnostic_duration_seconds.time():
            all_hypotheses = []
            for rule in self._rules:
                all_hypotheses.extend(rule.evaluate(evidence))

            ranked = sorted(all_hypotheses, key=lambda h: _CONFIDENCE_ORDER[h.confidence])

            result = DiagnosisResult(
                performed_at=datetime.now(timezone.utc),
                hypotheses=ranked,
                rules_evaluated=[r.name for r in self._rules],
            )

        top = result.top_hypothesis
        diagnostics_total.labels(reason_code=top.reason_code.value if top else "INCONCLUSIVE").inc()
        return result

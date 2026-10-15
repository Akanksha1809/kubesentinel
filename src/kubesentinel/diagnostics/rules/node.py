"""Node-level unschedulability: NotReady nodes, resource pressure, cordoning.

This produces a Node-kind incident (not correlated back to any specific Pod that
couldn't be scheduled because of it) -- see ARCHITECTURE.md for why IncidentManager's
UID-based dedup works for any resource kind, not just Pods. Correlating a pending
Pod to the specific Node condition that's blocking it is left to
PendingSchedulingRule's message-text classification (the scheduler's own
"X/Y nodes are available: ..." message), which already runs against Pod evidence
independently of this rule.
"""

from __future__ import annotations

from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.diagnostics.rules.base import DiagnosticRule

_PRESSURE_CONDITION_TYPES = {"MemoryPressure", "DiskPressure", "PIDPressure"}


class NodeConditionRule(DiagnosticRule):
    name = "node_condition"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        node_evidence = [e for e in evidence if e.kind == EvidenceKind.NODE_CONDITION]

        for ev in node_evidence:
            if ev.source_field == "status.conditions[Ready]" and ev.data.get("status") != "True":
                hypotheses.append(
                    Hypothesis(
                        rule_name=self.name,
                        reason_code=ReasonCode.NODE_UNSCHEDULABLE,
                        confidence=Confidence.HIGH,
                        explanation=(
                            f"Node reports Ready={ev.data.get('status')} "
                            f"({ev.data.get('reason') or 'no reason'}): "
                            f"{ev.data.get('message') or 'no message'}. New Pods cannot be "
                            "scheduled onto a Node that isn't Ready, and existing Pods on it may "
                            "be evicted."
                        ),
                        supporting_evidence=[ev.id],
                        missing_evidence=[],
                        recommended_next_steps=[
                            "Check kubelet health and node-level resource/network status "
                            "directly on this node.",
                        ],
                        auto_remediation_permitted=False,
                    )
                )
            elif ev.data.get("type") in _PRESSURE_CONDITION_TYPES and ev.data.get("status") == "True":
                hypotheses.append(
                    Hypothesis(
                        rule_name=self.name,
                        reason_code=ReasonCode.NODE_UNSCHEDULABLE,
                        confidence=Confidence.HIGH,
                        explanation=(
                            f"Node reports {ev.data.get('type')}=True: {ev.data.get('message') or 'no message'}. "
                            "The kubelet will not schedule additional Pods here under this pressure, "
                            "and may evict existing ones."
                        ),
                        supporting_evidence=[ev.id],
                        missing_evidence=[],
                        recommended_next_steps=[
                            "Check actual resource usage on this node and whether workloads "
                            "should be rebalanced or node capacity increased.",
                        ],
                        auto_remediation_permitted=False,
                    )
                )
            elif ev.source_field == "spec.unschedulable" and ev.data.get("unschedulable") is True:
                hypotheses.append(
                    Hypothesis(
                        rule_name=self.name,
                        reason_code=ReasonCode.NODE_UNSCHEDULABLE,
                        confidence=Confidence.MEDIUM,
                        explanation=(
                            "Node is cordoned (spec.unschedulable=true). This is often deliberate "
                            "maintenance rather than a failure -- confidence is capped at MEDIUM "
                            "because this evidence alone can't distinguish the two."
                        ),
                        supporting_evidence=[ev.id],
                        missing_evidence=["confirmation of whether this cordon was intentional"],
                        recommended_next_steps=[
                            "Check whether this node was deliberately cordoned for maintenance "
                            "before treating it as a problem.",
                        ],
                        auto_remediation_permitted=False,
                    )
                )

        return hypotheses

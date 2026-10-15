"""Turns a diagnosed hypothesis into advisory recommendation text.

Deliberately a pure function with no new content: it only ever reformats text a
diagnostic rule already produced (`explanation` + `recommended_next_steps`) into
one recommendation string -- it never invents new advice the rule didn't already
state, which is the same "never fabricate" discipline the rules themselves follow.
"""

from __future__ import annotations

from kubesentinel.models.action import ActionType
from kubesentinel.models.diagnosis import Hypothesis, ReasonCode

# Only reason codes where a concrete, actionable recommendation exists are mapped
# here. A reason code not in this mapping (e.g. CRASH_LOOP_BACKOFF, which is a
# symptom with no single fix -- see DIAGNOSTIC_RULES.md) has nothing specific to
# recommend beyond what diagnosis already said, so no recommendation action is built.
_RESOURCE_ADJUSTMENT_REASON_CODES = {ReasonCode.OOM_KILLED, ReasonCode.PENDING_INSUFFICIENT_RESOURCES}
_CONFIG_FIX_REASON_CODES = {
    ReasonCode.IMAGE_PULL_BACKOFF,
    ReasonCode.ERR_IMAGE_PULL,
    ReasonCode.MISSING_CONFIG_REFERENCE,
    ReasonCode.READINESS_PROBE_FAILING,
    ReasonCode.LIVENESS_PROBE_FAILING,
    ReasonCode.SERVICE_SELECTOR_MISMATCH,
    ReasonCode.PVC_BINDING_FAILURE,
    ReasonCode.PENDING_SCHEDULING_CONSTRAINT,
}


def build_recommendation(hypothesis: Hypothesis) -> tuple[ActionType, str] | None:
    """Returns (action_type, recommendation_text) for this hypothesis, or None if
    its reason code has no mapped advisory action type."""
    if hypothesis.reason_code in _RESOURCE_ADJUSTMENT_REASON_CODES:
        action_type = ActionType.RECOMMEND_RESOURCE_ADJUSTMENT
    elif hypothesis.reason_code in _CONFIG_FIX_REASON_CODES:
        action_type = ActionType.RECOMMEND_CONFIG_FIX
    else:
        return None

    lines = [hypothesis.explanation]
    if hypothesis.recommended_next_steps:
        lines.append("Next steps:")
        lines.extend(f"- {step}" for step in hypothesis.recommended_next_steps)
    return action_type, "\n".join(lines)

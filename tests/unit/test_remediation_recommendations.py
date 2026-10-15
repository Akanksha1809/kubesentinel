from kubesentinel.models.action import ActionType
from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.remediation.recommendations import build_recommendation


def _hypothesis(reason_code: ReasonCode, explanation: str = "test explanation", steps: list[str] | None = None) -> Hypothesis:
    return Hypothesis(
        rule_name="test_rule",
        reason_code=reason_code,
        confidence=Confidence.HIGH,
        explanation=explanation,
        recommended_next_steps=steps or [],
    )


def test_oom_killed_maps_to_resource_adjustment():
    hyp = _hypothesis(ReasonCode.OOM_KILLED)
    result = build_recommendation(hyp)
    assert result is not None
    action_type, text = result
    assert action_type == ActionType.RECOMMEND_RESOURCE_ADJUSTMENT
    assert "test explanation" in text


def test_image_pull_backoff_maps_to_config_fix():
    hyp = _hypothesis(ReasonCode.IMAGE_PULL_BACKOFF)
    action_type, _ = build_recommendation(hyp)
    assert action_type == ActionType.RECOMMEND_CONFIG_FIX


def test_crash_loop_backoff_has_no_mapped_recommendation():
    # A symptom with no single fix -- nothing specific to recommend.
    hyp = _hypothesis(ReasonCode.CRASH_LOOP_BACKOFF)
    assert build_recommendation(hyp) is None


def test_recommendation_text_includes_next_steps():
    hyp = _hypothesis(ReasonCode.OOM_KILLED, steps=["Raise the memory limit", "Check for a leak"])
    _, text = build_recommendation(hyp)
    assert "Raise the memory limit" in text
    assert "Check for a leak" in text


def test_recommendation_never_invents_text_beyond_the_hypothesis():
    hyp = _hypothesis(ReasonCode.OOM_KILLED, explanation="exact rule text", steps=[])
    _, text = build_recommendation(hyp)
    assert text == "exact rule text"

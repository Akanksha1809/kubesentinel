from datetime import datetime, timezone

import pytest

from kubesentinel.ai.client import AiClientError
from kubesentinel.ai.summarizer import build_prompt, summarize_incident
from kubesentinel.models.action import (
    ActionStatus,
    ActionTarget,
    ActionType,
    RemediationAction,
    VerificationOutcome,
    VerificationStatus,
)
from kubesentinel.models.diagnosis import Confidence, DiagnosisResult, Hypothesis, ReasonCode
from kubesentinel.models.incident import Incident, IncidentSeverity, IncidentStatus, ResourceRef


def _incident(hypotheses: list[Hypothesis] | None = None, actions: list[RemediationAction] | None = None) -> Incident:
    now = datetime.now(timezone.utc)
    diagnosis_history = []
    if hypotheses is not None:
        diagnosis_history.append(DiagnosisResult(performed_at=now, hypotheses=hypotheses))
    return Incident(
        incident_id="incident-1",
        resource=ResourceRef(namespace="kubesentinel-lab", kind="Pod", name="demo-pod", uid="uid-1"),
        category="CRASH_LOOP_BACKOFF",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.DIAGNOSED,
        first_observed_at=now,
        last_observed_at=now,
        diagnosis_history=diagnosis_history,
        actions=actions or [],
    )


class _FakeClient:
    def __init__(self, response: str | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.last_prompt: str | None = None

    def generate(self, prompt: str) -> str:
        self.last_prompt = prompt
        if self._error:
            raise self._error
        return self._response or "a summary"


def test_build_prompt_includes_hypothesis_already_ranked_order_unchanged():
    # Two hypotheses: HIGH first, MEDIUM second -- DiagnosticEngine always
    # stores diagnosis_history.hypotheses already ranked best-first, and
    # build_prompt must preserve that order, never re-derive it from
    # Confidence.value (which would sort alphabetically: "high" < "medium").
    high = Hypothesis(
        rule_name="crash_loop_backoff",
        reason_code=ReasonCode.CRASH_LOOP_BACKOFF,
        confidence=Confidence.HIGH,
        explanation="observed CrashLoopBackOff",
    )
    medium = Hypothesis(
        rule_name="oom_killed",
        reason_code=ReasonCode.OOM_KILLED,
        confidence=Confidence.MEDIUM,
        explanation="inferred from exit code 137",
        missing_evidence=["explicit OOMKilled reason"],
    )
    incident = _incident(hypotheses=[high, medium])

    prompt = build_prompt(incident)

    assert prompt.index("CRASH_LOOP_BACKOFF") < prompt.index("OOM_KILLED")
    assert "missing: explicit OOMKilled reason" in prompt


def test_build_prompt_handles_inconclusive_diagnosis():
    incident = _incident(hypotheses=[])
    prompt = build_prompt(incident)
    assert "inconclusive" in prompt.lower()


def test_build_prompt_includes_rationale_for_never_auto_authorizing():
    incident = _incident(hypotheses=[])
    prompt = build_prompt(incident)
    assert "policy engine" in prompt.lower()
    assert "never recommend" in prompt.lower() or "never" in prompt.lower()


def test_build_prompt_includes_prior_actions():
    action = RemediationAction(
        action_id="action-1",
        incident_id="incident-1",
        action_type=ActionType.RESTART_DEPLOYMENT,
        target=ActionTarget(namespace="kubesentinel-lab", resource_type="Deployment", resource_name="demo", resource_uid="uid-2"),
        status=ActionStatus.VERIFICATION_FAILED,
        rationale="test",
        proposed_at=datetime.now(timezone.utc),
        verification=VerificationOutcome(
            checked_at=datetime.now(timezone.utc),
            status=VerificationStatus.FAILED,
            reason="still broken",
        ),
    )
    incident = _incident(hypotheses=[], actions=[action])

    prompt = build_prompt(incident)

    assert "RESTART_DEPLOYMENT" in prompt
    assert "VERIFICATION_FAILED" in prompt
    assert "still broken" in prompt


def test_summarize_incident_returns_wrapped_result_with_disclaimer():
    # Stripping whitespace is the AiClient implementation's own job (see
    # OllamaClient.generate(), covered by test_ai_client.py) -- the fake here
    # returns already-clean text, matching the Protocol's implicit contract.
    client = _FakeClient(response="the pod keeps crashing")
    incident = _incident(hypotheses=[])

    summary = summarize_incident(incident, client, "qwen2.5-coder:3b")

    assert summary.incident_id == "incident-1"
    assert summary.text == "the pod keeps crashing"
    assert summary.model == "qwen2.5-coder:3b"
    assert "advisory" in summary.disclaimer.lower()
    assert client.last_prompt is not None and "inconclusive" in client.last_prompt.lower()


def test_summarize_incident_propagates_backend_errors():
    client = _FakeClient(error=AiClientError("ollama unreachable"))
    incident = _incident(hypotheses=[])

    with pytest.raises(AiClientError):
        summarize_incident(incident, client, "qwen2.5-coder:3b")

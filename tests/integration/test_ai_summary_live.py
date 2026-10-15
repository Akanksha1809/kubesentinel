"""Live check against a real local Ollama server -- not mocked. Skips (never
fails) if Ollama isn't reachable: this is a genuinely optional feature, and
`--all` must stay runnable on a machine without it installed.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from kubesentinel.ai.client import OllamaClient
from kubesentinel.ai.summarizer import summarize_incident
from kubesentinel.config import Settings
from kubesentinel.models.diagnosis import Confidence, DiagnosisResult, Hypothesis, ReasonCode
from kubesentinel.models.incident import Incident, IncidentSeverity, IncidentStatus, ResourceRef
from datetime import datetime, timezone


@pytest.fixture(scope="module")
def ollama_settings() -> Settings:
    settings = Settings()
    try:
        urllib.request.urlopen(f"{settings.ai_base_url}/api/version", timeout=3)
    except urllib.error.URLError as exc:
        pytest.skip(f"no local Ollama server reachable at {settings.ai_base_url}: {exc}")
    return settings


def test_summarize_real_incident_against_live_ollama(ollama_settings):
    now = datetime.now(timezone.utc)
    hypothesis = Hypothesis(
        rule_name="crash_loop_backoff",
        reason_code=ReasonCode.CRASH_LOOP_BACKOFF,
        confidence=Confidence.HIGH,
        explanation="Container 'app' is in CrashLoopBackOff after 5 restarts, last exit_code=1.",
        missing_evidence=["container logs"],
    )
    incident = Incident(
        incident_id="live-test-incident",
        resource=ResourceRef(namespace="kubesentinel-lab", kind="Pod", name="crashloop-demo-xyz", uid="uid-1"),
        category="CRASH_LOOP_BACKOFF",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.DIAGNOSED,
        first_observed_at=now,
        last_observed_at=now,
        diagnosis_history=[DiagnosisResult(performed_at=now, hypotheses=[hypothesis])],
    )

    client = OllamaClient(ollama_settings.ai_base_url, ollama_settings.ai_model, timeout_seconds=60.0)
    summary = summarize_incident(incident, client, ollama_settings.ai_model)

    assert summary.text
    assert len(summary.text) > 20
    assert summary.incident_id == "live-test-incident"

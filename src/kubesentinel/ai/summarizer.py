"""Builds a prompt strictly from an incident's own already-computed hypotheses
and actions, and asks an `AiClient` to summarize it in plain English.

This is the one deliberate boundary the spec requires: AI only ever explains
what the diagnostic/policy engines already decided. `build_prompt()` never
includes raw Pod/Event/annotation text (which `inspector.py` already treats
as untrusted input, per RBAC_AND_THREAT_MODEL.md) -- only the structured
`Hypothesis`/`RemediationAction` fields a human would already see via
`kubesentinel evidence`/`kubesentinel incidents`, so there is nothing in the
prompt a workload could use to inject instructions into the model, and
nothing the model could "discover" that KubeSentinel's own rules didn't
already compute and cite evidence for.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kubesentinel.ai.client import AiClient
from kubesentinel.models.ai import AiSummary
from kubesentinel.models.incident import Incident

_PROMPT_TEMPLATE = """\
You are explaining an already-completed Kubernetes diagnosis to an operator, \
in plain English, in at most 3 short paragraphs.

Rules you must follow:
- Only restate and clarify the facts below. Never invent a cause, evidence, \
or next step that isn't already listed.
- Never recommend or imply that any action should be taken automatically or \
without human approval -- a separate policy engine, not you, is the only \
system authorized to decide that.
- If something below is explicitly uncertain (confidence MEDIUM or an item \
listed as missing evidence), say so -- don't round it up to certainty.

Incident: {category} on {kind} '{resource_name}' (severity {severity}).

Diagnosis (most confident first):
{hypotheses_text}

Remediation actions taken so far (if any):
{actions_text}

Write the summary now.
"""


def build_prompt(incident: Incident) -> str:
    diagnosis = incident.latest_diagnosis
    if diagnosis and diagnosis.hypotheses:
        # Already ranked best-first by DiagnosticEngine.diagnose() -- never
        # re-sort here (re-deriving the rank from Confidence.value's string
        # would sort alphabetically, not by actual severity).
        hypotheses_text = "\n".join(
            f"- [{h.confidence.value.upper()}] {h.reason_code.value}: {h.explanation}"
            + (f" (missing: {'; '.join(h.missing_evidence)})" if h.missing_evidence else "")
            for h in diagnosis.hypotheses
        )
    else:
        hypotheses_text = "- No hypothesis has been confirmed yet; diagnosis is inconclusive."

    if incident.actions:
        actions_text = "\n".join(
            f"- {a.action_type.value} -> {a.status.value}"
            + (f" ({a.verification.reason})" if a.verification else "")
            for a in incident.actions
        )
    else:
        actions_text = "- None."

    return _PROMPT_TEMPLATE.format(
        category=incident.category,
        kind=incident.resource.kind,
        resource_name=incident.resource.name,
        severity=incident.severity.value,
        hypotheses_text=hypotheses_text,
        actions_text=actions_text,
    )


def summarize_incident(incident: Incident, client: AiClient, model_name: str) -> AiSummary:
    """Raises `ai.client.AiClientError` on any backend failure -- callers
    (the API endpoint, the CLI command) decide how to present that; this
    function never silently returns a fabricated summary."""
    prompt = build_prompt(incident)
    text = client.generate(prompt)
    return AiSummary(
        incident_id=incident.incident_id,
        text=text,
        model=model_name,
        generated_at=datetime.now(timezone.utc),
    )

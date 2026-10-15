"""Readiness and liveness probe failures.

Neither of these shows up in containerStatuses at all in the way a crash does:

- A failing **readiness** probe never restarts the container and never sets a
  waiting/terminated reason -- the container keeps running, it's just removed from
  Service endpoints (Ready=False) until the probe passes again. The *only* evidence
  of this is the Warning Event Kubernetes emits (`reason=Unhealthy`,
  `message="Readiness probe failed: ..."`).
- A failing **liveness** probe causes the kubelet to kill and restart the container
  (which *will* eventually also produce a CrashLoopBackOff waiting reason if it keeps
  failing) -- but the *reason it's restarting* is only named in the matching Warning
  Event (`reason=Unhealthy`, `message="Liveness probe failed: ..."`). Without the
  Event, this rule set has no way to distinguish "liveness probe is killing this
  container" from a plain application crash.

Both reasons are reported as the event reason "Unhealthy" by Kubernetes itself; this
module classifies which one by the message text, same approach as ImagePullRule.
"""

from __future__ import annotations

from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.diagnostics.rules.base import DiagnosticRule


class ReadinessProbeFailingRule(DiagnosticRule):
    name = "readiness_probe_failing"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        for ev in _unhealthy_events(evidence):
            message = ev.data.get("message") or ""
            if "readiness probe failed" not in message.lower():
                continue
            hypotheses.append(
                Hypothesis(
                    rule_name=self.name,
                    reason_code=ReasonCode.READINESS_PROBE_FAILING,
                    confidence=Confidence.HIGH,
                    explanation=(
                        f"Readiness probe is failing: {message}. The container keeps running "
                        "(this does not restart it) but is removed from Service endpoints while "
                        "it fails -- traffic routed to this Pod will drop until the probe passes."
                    ),
                    supporting_evidence=[ev.id],
                    missing_evidence=[],
                    recommended_next_steps=[
                        "Check whether the readiness probe's path/port/command actually reflects "
                        "the application's real readiness, and whether the app is slow to "
                        "initialize relative to the probe's initialDelaySeconds.",
                    ],
                    auto_remediation_permitted=False,
                )
            )
        return hypotheses


class LivenessProbeFailingRule(DiagnosticRule):
    name = "liveness_probe_failing"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        for ev in _unhealthy_events(evidence):
            message = ev.data.get("message") or ""
            if "liveness probe failed" not in message.lower():
                continue
            hypotheses.append(
                Hypothesis(
                    rule_name=self.name,
                    reason_code=ReasonCode.LIVENESS_PROBE_FAILING,
                    confidence=Confidence.HIGH,
                    explanation=(
                        f"Liveness probe is failing: {message}. Unlike a readiness failure, this "
                        "causes the kubelet to kill and restart the container -- if it keeps "
                        "failing, this is the actual cause behind what will otherwise look like a "
                        "generic CrashLoopBackOff."
                    ),
                    supporting_evidence=[ev.id],
                    missing_evidence=[],
                    recommended_next_steps=[
                        "Check whether the liveness probe's timeout/period is too aggressive for "
                        "the application, or whether the application is genuinely hung/deadlocked.",
                        "A restart from this probe will not help if the application is "
                        "deterministically too slow to respond -- fix the probe configuration or "
                        "the application's responsiveness, not just the restart.",
                    ],
                    auto_remediation_permitted=False,
                )
            )
        return hypotheses


def _unhealthy_events(evidence: list[Evidence]) -> list[Evidence]:
    return [e for e in evidence if e.kind == EvidenceKind.EVENT and e.data.get("reason") == "Unhealthy"]

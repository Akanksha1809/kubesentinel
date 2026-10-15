"""CrashLoopBackOff: the kubelet is backing off restarts of a repeatedly-failing container.

CrashLoopBackOff is a *symptom*, not a root cause -- it is the kubelet's reaction to a
container exiting repeatedly, whatever the reason. This rule therefore does not claim
certainty about why the container is crashing; it reports the backoff itself plus
whatever the last known termination reason/exit code were, and explicitly lists that
root-cause determination may require another rule (e.g. OOMKilledRule) or container
logs that aren't available to it.
"""

from __future__ import annotations

from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.diagnostics.rules.base import DiagnosticRule


class CrashLoopBackOffRule(DiagnosticRule):
    name = "crash_loop_backoff"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        waiting_states = [
            e
            for e in evidence
            if e.kind == EvidenceKind.CONTAINER_STATE
            and e.data.get("waiting_reason") == "CrashLoopBackOff"
        ]

        terminations_by_container = {
            e.data.get("container"): e
            for e in evidence
            if e.kind == EvidenceKind.CONTAINER_TERMINATION
        }

        for ev in waiting_states:
            container = ev.data.get("container")
            restart_count = ev.data.get("restart_count", 0)
            termination = terminations_by_container.get(container)

            supporting = [ev.id]
            missing: list[str] = []
            last_reason = None
            last_exit_code = None

            if termination is not None:
                supporting.append(termination.id)
                last_reason = termination.data.get("terminated_reason") or termination.data.get(
                    "last_terminated_reason"
                )
                last_exit_code = termination.data.get("exit_code") or termination.data.get(
                    "last_exit_code"
                )
            else:
                missing.append("last termination reason/exit code for this container")

            missing.append("container logs (not yet collected by this rule)")

            explanation = (
                f"Container '{container}' is in CrashLoopBackOff after {restart_count} restarts. "
                "This means the kubelet is backing off restarts of a repeatedly failing container "
                "-- it is a symptom, not the root cause by itself."
            )
            if last_reason:
                explanation += f" Last known termination reason: {last_reason} (exit_code={last_exit_code})."
                if last_reason == "OOMKilled":
                    explanation += " See the OOMKilled hypothesis for the likely root cause."
                elif last_exit_code == 0:
                    explanation += (
                        " Exit code 0 with repeated restarts suggests the process is exiting "
                        "cleanly but something (e.g. a missing long-running task) causes it to "
                        "keep exiting -- check the container's entrypoint/command."
                    )
                else:
                    explanation += (
                        " A non-zero exit code with no OOM signal suggests an application-level "
                        "crash or misconfiguration; container logs are needed to confirm."
                    )
            else:
                explanation += " No termination record was available to narrow down why it's crashing."

            confidence = Confidence.HIGH if restart_count >= 3 else Confidence.MEDIUM

            hypotheses.append(
                Hypothesis(
                    rule_name=self.name,
                    reason_code=ReasonCode.CRASH_LOOP_BACKOFF,
                    confidence=confidence,
                    explanation=explanation,
                    supporting_evidence=supporting,
                    missing_evidence=missing,
                    recommended_next_steps=[
                        "Inspect current and previous container logs for the crash reason.",
                        "Check readiness/liveness probe configuration if probes could be killing it.",
                        "Do not rely on restarts alone -- a restart will not fix a deterministic "
                        "crash, missing config, or bad image.",
                    ],
                    auto_remediation_permitted=False,
                )
            )

        return hypotheses

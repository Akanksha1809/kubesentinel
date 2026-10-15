"""OOMKilled: a container was terminated by the kernel OOM killer.

The authoritative signal is containerStatuses[].state.terminated.reason == "OOMKilled"
(or lastState.terminated while the container is restarting) -- the kubelet sets this
specifically when it detects an OOM kill, so this rule treats it as HIGH confidence,
an observed fact rather than an inference.

In practice, not every kubelet/container-runtime/cgroup-driver combination reliably
sets that reason even when an OOM kill genuinely happened (verified independently via
cgroup v2 memory.events' oom_kill counter against this project's own kind lab, where
exit_code=137 was reported under the generic reason "Error"). This rule therefore also
recognizes exit_code 137 with repeated restarts as a *lower-confidence, inferred*
hypothesis -- explicitly labeled as inferred, never presented with the same certainty
as the directly-reported reason. This is exactly the observed-vs-inferred distinction
the diagnostic engine is built to preserve.
"""

from __future__ import annotations

from kubesentinel.models.diagnosis import Confidence, Hypothesis, ReasonCode
from kubesentinel.models.evidence import Evidence, EvidenceKind
from kubesentinel.diagnostics.rules.base import DiagnosticRule

_SIGKILL_EXIT_CODE = 137
_MIN_RESTARTS_FOR_INFERRED_OOM = 2


class OOMKilledRule(DiagnosticRule):
    name = "oom_killed"

    def evaluate(self, evidence: list[Evidence]) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        terminations = [e for e in evidence if e.kind == EvidenceKind.CONTAINER_TERMINATION]

        for ev in terminations:
            reason = ev.data.get("terminated_reason") or ev.data.get("last_terminated_reason")
            exit_code = ev.data.get("exit_code") or ev.data.get("last_exit_code")
            container = ev.data.get("container")
            restart_count = ev.data.get("restart_count", 0)

            if reason == "OOMKilled":
                hypotheses.append(self._observed_oom(ev, container, exit_code))
            elif exit_code == _SIGKILL_EXIT_CODE and restart_count >= _MIN_RESTARTS_FOR_INFERRED_OOM:
                hypotheses.append(self._inferred_oom(ev, container, restart_count))

        return hypotheses

    def _observed_oom(self, ev: Evidence, container: str | None, exit_code: int | None) -> Hypothesis:
        return Hypothesis(
            rule_name=self.name,
            reason_code=ReasonCode.OOM_KILLED,
            confidence=Confidence.HIGH,
            explanation=(
                f"Container '{container}' was terminated with reason OOMKilled "
                f"(exit_code={exit_code}). The container exceeded its memory limit "
                "and was killed by the kernel. Restarting it without raising the "
                "memory limit or fixing the memory usage will likely repeat this."
            ),
            supporting_evidence=[ev.id],
            missing_evidence=["container resource limits (not yet collected)"],
            recommended_next_steps=[
                "Inspect container resource limits and actual memory usage.",
                "Raise the memory limit if the workload's real usage exceeds it, "
                "or fix a memory leak if usage grows unbounded over time.",
            ],
            auto_remediation_permitted=False,
        )

    def _inferred_oom(self, ev: Evidence, container: str | None, restart_count: int) -> Hypothesis:
        return Hypothesis(
            rule_name=self.name,
            reason_code=ReasonCode.OOM_KILLED,
            confidence=Confidence.MEDIUM,
            explanation=(
                f"Container '{container}' was terminated with exit_code=137 (SIGKILL) across "
                f"{restart_count} restarts, but the container runtime reported the generic "
                "reason 'Error' rather than 'OOMKilled'. This is INFERRED, not directly "
                "observed: exit_code 137 with a tight restart loop is consistent with an "
                "OOM kill, but could also be an external SIGKILL (e.g. node pressure eviction, "
                "manual kill). Some kubelet/runtime/cgroup-driver combinations do not reliably "
                "surface the OOMKilled reason even when the kernel OOM killer fired."
            ),
            supporting_evidence=[ev.id],
            missing_evidence=[
                "explicit OOMKilled reason from the container runtime",
                "node-level cgroup memory.events oom_kill counter for this container",
            ],
            recommended_next_steps=[
                "Check node-level cgroup memory.events (oom_kill counter) or dmesg for an "
                "OOM kill matching this container's timing.",
                "Inspect container resource limits and actual memory usage.",
            ],
            auto_remediation_permitted=False,
        )

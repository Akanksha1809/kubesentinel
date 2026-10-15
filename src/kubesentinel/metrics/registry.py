"""Prometheus metric definitions.

Labels are kept to a small, bounded set of known values (incident category, action
type, outcome) -- never incident IDs, pod names, or other unbounded user-controlled
strings, which would blow up cardinality in Prometheus.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

incidents_detected_total = Counter(
    "kubesentinel_incidents_detected_total",
    "Total incidents opened, by category.",
    ["category"],
)

incidents_active = Gauge(
    "kubesentinel_incidents_active",
    "Currently open (non-resolved) incidents, by category.",
    ["category"],
)

diagnostics_total = Counter(
    "kubesentinel_diagnostics_total",
    "Total diagnostic engine runs, by top result reason code.",
    ["reason_code"],
)

diagnostic_duration_seconds = Histogram(
    "kubesentinel_diagnostic_duration_seconds",
    "Time spent running the diagnostic engine on one evidence set.",
)

remediation_attempts_total = Counter(
    "kubesentinel_remediation_attempts_total",
    "Remediation actions attempted, by action type.",
    ["action_type"],
)

remediation_success_total = Counter(
    "kubesentinel_remediation_success_total",
    "Remediation actions verified as successful, by action type.",
    ["action_type"],
)

remediation_failures_total = Counter(
    "kubesentinel_remediation_failures_total",
    "Remediation actions that failed to execute or failed verification, by action type and reason.",
    ["action_type", "reason"],
)

recovery_duration_seconds = Histogram(
    "kubesentinel_recovery_duration_seconds",
    "Time from incident first_observed_at to resolution, by category.",
    ["category"],
)

actions_requiring_approval = Counter(
    "kubesentinel_actions_requiring_approval",
    "Proposed actions that required explicit approval, by action type.",
    ["action_type"],
)

observer_errors_total = Counter(
    "kubesentinel_observer_errors_total",
    "Errors encountered by the cluster observer watch loops, by component.",
    ["component"],
)

ai_summaries_total = Counter(
    "kubesentinel_ai_summaries_total",
    "AI summaries successfully generated.",
)

ai_errors_total = Counter(
    "kubesentinel_ai_errors_total",
    "AI summary requests that failed (backend unreachable, timed out, or malformed response).",
)

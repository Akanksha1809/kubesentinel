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

optimization_analysis_total = Counter(
    "kubesentinel_optimization_analysis_total",
    "Optimization analyze() runs, by outcome: success (metrics data was available), "
    "partial (completed but usage metrics were missing/unreachable), or failed "
    "(the run itself raised, e.g. the cluster was unreachable).",
    ["status"],
)

optimization_recommendations_total = Counter(
    "kubesentinel_optimization_recommendations_total",
    "Recommendations produced by analyze() runs, by category.",
    ["category"],
)

optimization_last_success_timestamp = Gauge(
    "kubesentinel_optimization_last_success_timestamp",
    "Unix timestamp of the last analyze() run that completed without raising "
    "(success or partial -- never set on a failed run, so a stale value here "
    "is an honest freshness signal even when metrics data itself is missing).",
)

optimization_metrics_data_available = Gauge(
    "kubesentinel_optimization_metrics_data_available",
    "Whether a usage data provider was available on the last analyze() run (1) or "
    "not (0), by provider. 'metrics_api' reflects this run's actual outcome; "
    "'prometheus' reflects only whether a Prometheus URL is configured "
    "(KUBESENTINEL_OPTIMIZATION_PROMETHEUS_URL) -- analyze_workload() queries it "
    "per-container, not once per run, so this is a configuration signal, not a "
    "last-query-succeeded signal.",
    ["provider"],
)

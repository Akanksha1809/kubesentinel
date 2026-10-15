"""Safety validation for optimization recommendations.

Every check here *flags*, never silently drops, a recommendation --
"Workloads that are unhealthy and should not be optimized until investigated"
(the spec's own wording) means exactly that: still reported, with a flag and
no `proposed_*` number, never silence. Silence would be indistinguishable
from "this workload was never looked at," which is a strictly worse failure
mode for an operator trying to trust what this engine does and doesn't cover.

This module never touches the Kubernetes API, a repository, or the clock on
its own -- every fact it needs is already on the `WorkloadSnapshot`/
`Settings` it's handed, the same "pure function over already-fetched facts"
shape as `policy/engine.py::PolicyEngine`.
"""

from __future__ import annotations

from kubesentinel.config import Settings
from kubesentinel.models.optimization import ResourceRecommendation, WorkloadKind, WorkloadSnapshot

# Workload kinds the spec explicitly asks to treat conservatively regardless
# of what their usage looks like -- a Job's resource needs are tied to one
# run, not a steady state, and a DaemonSet's "right" size is a per-node
# question this engine doesn't yet model (see
# RESOURCE_OPTIMIZATION_DESIGN.md's "deliberately not built" list).
_CONSERVATIVE_KINDS = {WorkloadKind.JOB, WorkloadKind.DAEMONSET}


def apply_safety_checks(
    workload: WorkloadSnapshot,
    recommendations: list[ResourceRecommendation],
    settings: Settings,  # noqa: ARG001 - kept for a consistent signature as this grows
    related_incident_ids: list[str] | None = None,
) -> list[ResourceRecommendation]:
    """Mutates and returns `recommendations`: adds safety flags and, when any
    apply, clears `proposed_requests`/`proposed_limits` (never the row
    itself, never `reasoning`/`risks`/other context).

    `related_incident_ids` is the already-resolved list of currently-open
    incident IDs for this workload (computed by the caller via
    `IncidentRepository.find_open_by_resource_uid`, walked over the
    workload's own UID and its current Pods' UIDs -- this module stays a
    pure function over already-fetched facts and never queries a repository
    itself, same as every other check here)."""
    for recommendation in recommendations:
        flags = list(recommendation.safety_flags)
        suppress_proposal = False

        if workload.is_system_namespace:
            flags.append("SYSTEM_NAMESPACE")
            suppress_proposal = True
        if not workload.healthy:
            flags.append("UNHEALTHY_WORKLOAD")
            suppress_proposal = True
        if workload.workload_kind in _CONSERVATIVE_KINDS:
            flags.append(f"{workload.workload_kind.value.upper()}_CONSERVATIVE")
            suppress_proposal = True
        if related_incident_ids:
            # An open incident is a reason to defer to the troubleshooting
            # engine first, not a reason to hide the recommendation -- the
            # row stays visible, linked to the incident(s), with no proposed
            # number until the incident is resolved.
            flags.append("ACTIVE_INCIDENT")
            suppress_proposal = True
            recommendation.related_incident_ids = list(related_incident_ids)

        if suppress_proposal:
            recommendation.proposed_requests = None
            recommendation.proposed_limits = None

        recommendation.safety_flags = flags

    return recommendations

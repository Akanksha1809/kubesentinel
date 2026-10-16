"""Deciding when a pod's evidence indicates failure, and separately, confirmed recovery.

These are deliberately two different, asymmetric checks, not one boolean negated:

- Opening a new incident requires a *specific known-bad* signal (`has_failure_signal`):
  a waiting reason like CrashLoopBackOff, or an OOMKilled termination. A Pod that's
  simply still starting (ContainerCreating, not yet Ready) must not open an incident
  just because it isn't confirmed-healthy yet.
- Resolving an *already open* incident requires positive, confirmed health
  (`is_confirmed_healthy`): every container currently Running and Ready. This was
  found necessary live, not assumed up front: a container that just crashed can
  transiently report `state.terminated` with a generic reason (e.g. "Error") for one
  watch event, *before* the kubelet relabels it `waiting: CrashLoopBackOff` for the
  backoff period. `has_failure_signal` alone doesn't catch that generic-reason instant
  (it isn't one of the known-bad waiting/terminated reasons), so it would otherwise
  look like a transient "no problem" Pod wake me up when something crashes
  moment -- and the incident would be prematurely resolved, then immediately
  reopened on the next watch event. Requiring `ready=True` (which the kubelet never
  sets while a container isn't actually running) closes that gap.
"""

from __future__ import annotations

from kubesentinel.models.evidence import Evidence, EvidenceKind

_BAD_WAITING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "CreateContainerError",
}
_BAD_TERMINATION_REASONS = {"OOMKilled"}
# Event reasons that indicate a problem even when no container waiting/terminated
# state reflects it -- a failing readiness probe never restarts the container or sets
# a waiting reason, it only ever shows up as this Event. ProvisioningFailed/
# FailedBinding are PVC binding failures (see diagnostics/rules/pvc.py for why PVC
# `Pending` phase alone is deliberately NOT included here). FailedMount is a Pod
# stuck unable to mount an already-Bound volume -- a different failure mode from
# binding (see diagnostics/rules/mount.py); a container that never mounts never
# reports a waiting/terminated reason either, so without this it would stay
# invisible to has_failure_signal and never open an incident at all.
_BAD_EVENT_REASONS = {"Unhealthy", "ProvisioningFailed", "FailedBinding", "FailedMount"}
_NODE_PRESSURE_CONDITIONS = {"MemoryPressure", "DiskPressure", "PIDPressure"}


def has_failure_signal(evidence: list[Evidence]) -> bool:
    for e in evidence:
        if e.kind == EvidenceKind.CONTAINER_STATE and e.data.get("waiting_reason") in _BAD_WAITING_REASONS:
            return True
        if e.kind == EvidenceKind.CONTAINER_TERMINATION:
            reason = e.data.get("terminated_reason") or e.data.get("last_terminated_reason")
            if reason in _BAD_TERMINATION_REASONS:
                return True
        if (
            e.kind == EvidenceKind.POD_CONDITION
            and e.data.get("type") == "PodScheduled"
            and e.data.get("status") == "False"
            and e.data.get("reason") == "Unschedulable"
        ):
            return True
        if e.kind == EvidenceKind.EVENT and e.data.get("reason") in _BAD_EVENT_REASONS:
            return True
        if (
            e.kind == EvidenceKind.DEPLOYMENT_STATUS
            and e.data.get("type") == "Progressing"
            and e.data.get("status") == "False"
            and e.data.get("reason") == "ProgressDeadlineExceeded"
        ):
            return True
        if (
            e.kind == EvidenceKind.DEPLOYMENT_STATUS
            and e.data.get("type") == "Available"
            and e.data.get("status") == "False"
        ):
            # Confirmed live against this project's own lab: a Deployment whose pods
            # start crashing *after* a successful initial rollout shows
            # Progressing=True/NewReplicaSetAvailable (the rollout itself genuinely
            # succeeded) but Available=False/MinimumReplicasUnavailable -- the signal
            # that actually reflects the ongoing problem. Known tradeoff: a brand-new
            # Deployment is also briefly Available=False while its first replicas
            # start, so this can open a short-lived incident that resolves within a
            # few observations once it reaches minimum availability -- see TODO.md.
            return True
        if e.kind == EvidenceKind.NODE_CONDITION:
            if e.source_field == "status.conditions[Ready]" and e.data.get("status") != "True":
                return True
            if e.data.get("type") in _NODE_PRESSURE_CONDITIONS and e.data.get("status") == "True":
                return True
            # Deliberately NOT included: spec.unschedulable=True (cordoned) alone.
            # Cordoning is routine, deliberate maintenance far more often than it's a
            # problem -- NodeConditionRule still reports it (capped at MEDIUM
            # confidence) once an incident is open for another reason, but it does
            # not by itself open one.
        if e.kind == EvidenceKind.ENDPOINTSLICE_STATE and e.data.get("total_endpoints") == 0:
            return True
    return False


def is_confirmed_healthy(evidence: list[Evidence]) -> bool:
    replica_snapshots = [
        e for e in evidence if e.kind == EvidenceKind.DEPLOYMENT_STATUS and e.source_field == "status.replicas"
    ]
    if replica_snapshots:
        latest = replica_snapshots[-1]
        return latest.data["ready_replicas"] >= latest.data["desired_replicas"]

    pvc_snapshots = [e for e in evidence if e.kind == EvidenceKind.PVC_STATUS]
    if pvc_snapshots:
        return pvc_snapshots[-1].data.get("phase") == "Bound"

    endpointslice_snapshots = [e for e in evidence if e.kind == EvidenceKind.ENDPOINTSLICE_STATE]
    if endpointslice_snapshots:
        return endpointslice_snapshots[-1].data.get("total_endpoints", 0) > 0

    node_conditions = [e for e in evidence if e.kind == EvidenceKind.NODE_CONDITION]
    if node_conditions:
        ready = [e for e in node_conditions if e.source_field == "status.conditions[Ready]"]
        if not ready or ready[-1].data.get("status") != "True":
            return False
        # Only the latest observation per pressure type matters -- a past pressure
        # spike that has since cleared must not keep the incident open forever.
        for pressure_type in _NODE_PRESSURE_CONDITIONS:
            latest_for_type = [e for e in node_conditions if e.data.get("type") == pressure_type]
            if latest_for_type and latest_for_type[-1].data.get("status") == "True":
                return False
        return True

    container_states = [e for e in evidence if e.kind == EvidenceKind.CONTAINER_STATE]
    if not container_states:
        return False

    containers = {e.data.get("container") for e in container_states}
    for container in containers:
        this_container = [e for e in container_states if e.data.get("container") == container]
        if not all(e.data.get("ready") is True for e in this_container):
            return False
    return True

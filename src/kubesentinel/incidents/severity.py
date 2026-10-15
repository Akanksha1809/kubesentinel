from __future__ import annotations

from kubesentinel.models.diagnosis import ReasonCode
from kubesentinel.models.incident import IncidentSeverity

_SEVERITY_BY_REASON: dict[ReasonCode, IncidentSeverity] = {
    ReasonCode.OOM_KILLED: IncidentSeverity.HIGH,
    ReasonCode.CRASH_LOOP_BACKOFF: IncidentSeverity.HIGH,
    ReasonCode.IMAGE_PULL_BACKOFF: IncidentSeverity.MEDIUM,
    ReasonCode.ERR_IMAGE_PULL: IncidentSeverity.MEDIUM,
    ReasonCode.PENDING_INSUFFICIENT_RESOURCES: IncidentSeverity.MEDIUM,
    ReasonCode.PENDING_SCHEDULING_CONSTRAINT: IncidentSeverity.MEDIUM,
    ReasonCode.READINESS_PROBE_FAILING: IncidentSeverity.MEDIUM,
    ReasonCode.LIVENESS_PROBE_FAILING: IncidentSeverity.HIGH,
    ReasonCode.ROLLOUT_FAILED: IncidentSeverity.HIGH,
    ReasonCode.UNSATISFIED_REPLICAS: IncidentSeverity.MEDIUM,
    ReasonCode.SERVICE_SELECTOR_MISMATCH: IncidentSeverity.MEDIUM,
    ReasonCode.DNS_RESOLUTION_FAILURE: IncidentSeverity.MEDIUM,
    ReasonCode.PVC_BINDING_FAILURE: IncidentSeverity.MEDIUM,
    ReasonCode.NODE_UNSCHEDULABLE: IncidentSeverity.MEDIUM,
    ReasonCode.MISSING_CONFIG_REFERENCE: IncidentSeverity.MEDIUM,
    ReasonCode.INCONCLUSIVE: IncidentSeverity.LOW,
}


def severity_for(reason_code: ReasonCode) -> IncidentSeverity:
    return _SEVERITY_BY_REASON.get(reason_code, IncidentSeverity.LOW)

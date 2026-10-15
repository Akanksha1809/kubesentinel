"""Evidence: an observed fact pulled from the Kubernetes API.

Evidence must always be something KubeSentinel actually read from the API or logs at
a specific time -- never a guess. Diagnostic rules consume Evidence; they never invent it.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


class EvidenceKind(str, Enum):
    POD_STATUS = "pod_status"
    CONTAINER_STATE = "container_state"
    CONTAINER_TERMINATION = "container_termination"
    POD_CONDITION = "pod_condition"
    # A Kubernetes Event about any object (Pod, PersistentVolumeClaim, ...) -- not
    # just Pods, despite how many of today's rules happen to use it for Pod-related
    # Events. Kept as one kind rather than one per involved-object-kind because every
    # rule that reads an Event filters on its `reason`/`message`, not its kind.
    EVENT = "event"
    DEPLOYMENT_STATUS = "deployment_status"
    REPLICASET_STATUS = "replicaset_status"
    NODE_CONDITION = "node_condition"
    SERVICE_SPEC = "service_spec"
    ENDPOINTSLICE_STATE = "endpointslice_state"
    PVC_STATUS = "pvc_status"
    CONFIG_REFERENCE = "config_reference"
    LOG_EXCERPT = "log_excerpt"
    RESOURCE_SPEC = "resource_spec"


class Evidence(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: EvidenceKind
    observed_at: datetime
    # Short machine field name, e.g. "containerStatuses[0].state.waiting.reason".
    source_field: str
    # Human-readable one-liner, e.g. "container 'app' waiting: ImagePullBackOff".
    summary: str
    # Structured payload for programmatic consumption by rules. Must not contain secret values.
    data: dict = Field(default_factory=dict)
    # Resource version / UID this evidence was read at, for staleness checks.
    resource_version: str | None = None

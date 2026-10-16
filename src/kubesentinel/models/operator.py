"""Typed parsing for the one CRD Milestone 8's operator reconciles --
`KubeSentinelRemediationPolicy`. `CustomObjectsApi` hands back a plain
dict with no schema validation of its own beyond what the CRD's
OpenAPI schema already rejected at admission time; this model is where
that dict becomes something the controller can work with by name, with
the same defaults `config.py::Settings` already uses for every field
this mirrors -- see docs/OPERATOR_DESIGN.md §4.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kubesentinel.config import PolicyMode


class RemediationPolicySpec(BaseModel):
    mode: PolicyMode
    cooldown_seconds: int = Field(default=60, alias="cooldownSeconds")
    max_actions_per_incident: int = Field(default=3, alias="maxActionsPerIncident")
    max_actions_per_window: int = Field(default=10, alias="maxActionsPerWindow")
    window_seconds: int = Field(default=600, alias="windowSeconds")
    allowed_resource_names: list[str] = Field(default_factory=list, alias="allowedResourceNames")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_cr(cls, cr: dict) -> RemediationPolicySpec:
        return cls.model_validate(cr.get("spec", {}))


class RemediationPolicyRef(BaseModel):
    """Identity of the CR a reconcile is for -- enough to PATCH its
    `.status` subresource back (`name`/`namespace`), and to skip a
    redundant reconcile when neither `generation` nor the targeted
    incidents have changed (not currently used for that -- see
    docs/OPERATOR_DESIGN.md's own "what this milestone does not
    attempt" -- but kept distinct from the raw dict so the controller
    never re-parses `cr["metadata"][...]` inline more than once)."""

    name: str
    namespace: str
    generation: int = 0

    @classmethod
    def from_cr(cls, cr: dict) -> RemediationPolicyRef:
        metadata = cr.get("metadata", {})
        return cls(name=metadata["name"], namespace=metadata["namespace"], generation=metadata.get("generation", 0))

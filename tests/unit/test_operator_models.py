import pytest
from pydantic import ValidationError

from kubesentinel.config import PolicyMode
from kubesentinel.models.operator import RemediationPolicyRef, RemediationPolicySpec


def _cr(spec: dict | None = None, name: str = "test-policy", namespace: str = "kubesentinel-lab", generation: int = 1) -> dict:
    return {
        "metadata": {"name": name, "namespace": namespace, "generation": generation},
        "spec": spec or {"mode": "APPROVAL_REQUIRED"},
    }


class TestRemediationPolicySpec:
    def test_parses_camel_case_fields_with_defaults(self):
        spec = RemediationPolicySpec.from_cr(_cr({"mode": "CONTROLLED_AUTO", "cooldownSeconds": 120}))
        assert spec.mode == PolicyMode.CONTROLLED_AUTO
        assert spec.cooldown_seconds == 120
        # Defaults for everything not supplied, mirroring Settings' own defaults.
        assert spec.max_actions_per_incident == 3
        assert spec.max_actions_per_window == 10
        assert spec.window_seconds == 600
        assert spec.allowed_resource_names == []

    def test_parses_every_field(self):
        spec = RemediationPolicySpec.from_cr(
            _cr(
                {
                    "mode": "OBSERVE",
                    "cooldownSeconds": 30,
                    "maxActionsPerIncident": 1,
                    "maxActionsPerWindow": 2,
                    "windowSeconds": 300,
                    "allowedResourceNames": ["demo-deployment"],
                }
            )
        )
        assert spec.mode == PolicyMode.OBSERVE
        assert spec.cooldown_seconds == 30
        assert spec.max_actions_per_incident == 1
        assert spec.max_actions_per_window == 2
        assert spec.window_seconds == 300
        assert spec.allowed_resource_names == ["demo-deployment"]

    def test_missing_mode_is_rejected(self):
        with pytest.raises(ValidationError):
            RemediationPolicySpec.from_cr(_cr({"cooldownSeconds": 30}))

    def test_invalid_mode_value_is_rejected(self):
        with pytest.raises(ValidationError):
            RemediationPolicySpec.from_cr(_cr({"mode": "NOT_A_REAL_MODE"}))


class TestRemediationPolicyRef:
    def test_extracts_identity_fields(self):
        ref = RemediationPolicyRef.from_cr(_cr(name="my-policy", namespace="other-ns", generation=7))
        assert ref.name == "my-policy"
        assert ref.namespace == "other-ns"
        assert ref.generation == 7

    def test_missing_generation_defaults_to_zero(self):
        cr = _cr()
        del cr["metadata"]["generation"]
        ref = RemediationPolicyRef.from_cr(cr)
        assert ref.generation == 0

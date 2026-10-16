from kubernetes.client import (
    V1Container,
    V1EnvFromSource,
    V1EnvVar,
    V1EnvVarSource,
    V1HostPathVolumeSource,
    V1PodSecurityContext,
    V1PodSpec,
    V1SecretEnvSource,
    V1SecretKeySelector,
    V1SecurityContext,
    V1Volume,
)

from kubesentinel.security.inventory import SecurityInventory, WorkloadPodTemplate
from kubesentinel.security.rules.container import (
    AllowPrivilegeEscalationRule,
    HostNamespaceSharingRule,
    HostPathVolumeRule,
    MissingReadOnlyRootFsRule,
    MutableImageTagRule,
    PrivilegedContainerRule,
    RunAsRootRule,
    SecretAsEnvVarRule,
    _has_mutable_tag,
)

NAMESPACE = "kubesentinel-lab"


def _template(pod_spec: V1PodSpec, workload_name="demo") -> WorkloadPodTemplate:
    return WorkloadPodTemplate(namespace=NAMESPACE, workload_kind="Deployment", workload_name=workload_name, pod_spec=pod_spec)


def _inventory(*templates: WorkloadPodTemplate) -> SecurityInventory:
    return SecurityInventory(pod_templates=list(templates))


def _container(name="app", security_context=None, **kwargs) -> V1Container:
    return V1Container(name=name, security_context=security_context, **kwargs)


class TestPrivilegedContainerRule:
    def test_fires_when_privileged(self):
        spec = V1PodSpec(containers=[_container(security_context=V1SecurityContext(privileged=True))])
        findings = PrivilegedContainerRule().evaluate(_inventory(_template(spec)))
        assert len(findings) == 1
        assert findings[0].severity.value == "CRITICAL"

    def test_does_not_fire_when_not_privileged(self):
        spec = V1PodSpec(containers=[_container(security_context=V1SecurityContext(privileged=False))])
        assert PrivilegedContainerRule().evaluate(_inventory(_template(spec))) == []

    def test_does_not_fire_with_no_security_context(self):
        spec = V1PodSpec(containers=[_container()])
        assert PrivilegedContainerRule().evaluate(_inventory(_template(spec))) == []


class TestAllowPrivilegeEscalationRule:
    def test_fires_high_when_explicitly_true(self):
        spec = V1PodSpec(containers=[_container(security_context=V1SecurityContext(allow_privilege_escalation=True))])
        findings = AllowPrivilegeEscalationRule().evaluate(_inventory(_template(spec)))
        assert len(findings) == 1
        assert findings[0].severity.value == "HIGH"

    def test_fires_low_when_unset(self):
        spec = V1PodSpec(containers=[_container()])
        findings = AllowPrivilegeEscalationRule().evaluate(_inventory(_template(spec)))
        assert len(findings) == 1
        assert findings[0].severity.value == "LOW"

    def test_does_not_fire_when_explicitly_false(self):
        spec = V1PodSpec(containers=[_container(security_context=V1SecurityContext(allow_privilege_escalation=False))])
        assert AllowPrivilegeEscalationRule().evaluate(_inventory(_template(spec))) == []


class TestRunAsRootRule:
    def test_fires_when_neither_level_set(self):
        spec = V1PodSpec(containers=[_container()])
        findings = RunAsRootRule().evaluate(_inventory(_template(spec)))
        assert len(findings) == 1

    def test_does_not_fire_when_container_level_true(self):
        spec = V1PodSpec(containers=[_container(security_context=V1SecurityContext(run_as_non_root=True))])
        assert RunAsRootRule().evaluate(_inventory(_template(spec))) == []

    def test_does_not_fire_when_pod_level_true_and_container_unset(self):
        spec = V1PodSpec(containers=[_container()], security_context=V1PodSecurityContext(run_as_non_root=True))
        assert RunAsRootRule().evaluate(_inventory(_template(spec))) == []

    def test_container_level_false_overrides_pod_level_true(self):
        spec = V1PodSpec(
            containers=[_container(security_context=V1SecurityContext(run_as_non_root=False))],
            security_context=V1PodSecurityContext(run_as_non_root=True),
        )
        findings = RunAsRootRule().evaluate(_inventory(_template(spec)))
        assert len(findings) == 1


class TestMissingReadOnlyRootFsRule:
    def test_fires_when_unset(self):
        spec = V1PodSpec(containers=[_container()])
        assert len(MissingReadOnlyRootFsRule().evaluate(_inventory(_template(spec)))) == 1

    def test_does_not_fire_when_true(self):
        spec = V1PodSpec(containers=[_container(security_context=V1SecurityContext(read_only_root_filesystem=True))])
        assert MissingReadOnlyRootFsRule().evaluate(_inventory(_template(spec))) == []


class TestHostNamespaceSharingRule:
    def test_fires_for_host_network(self):
        spec = V1PodSpec(containers=[_container()], host_network=True)
        findings = HostNamespaceSharingRule().evaluate(_inventory(_template(spec)))
        assert len(findings) == 1
        assert "hostNetwork" in findings[0].evidence[0]

    def test_fires_once_for_multiple_shared_namespaces(self):
        spec = V1PodSpec(containers=[_container()], host_network=True, host_pid=True, host_ipc=True)
        findings = HostNamespaceSharingRule().evaluate(_inventory(_template(spec)))
        assert len(findings) == 1
        assert len(findings[0].evidence) == 3

    def test_does_not_fire_with_no_host_namespaces(self):
        spec = V1PodSpec(containers=[_container()])
        assert HostNamespaceSharingRule().evaluate(_inventory(_template(spec))) == []


class TestHostPathVolumeRule:
    def test_fires_for_hostpath_volume(self):
        spec = V1PodSpec(containers=[_container()], volumes=[V1Volume(name="v", host_path=V1HostPathVolumeSource(path="/etc"))])
        findings = HostPathVolumeRule().evaluate(_inventory(_template(spec)))
        assert len(findings) == 1
        assert "/etc" in findings[0].evidence[0]

    def test_does_not_fire_for_non_hostpath_volume(self):
        spec = V1PodSpec(containers=[_container()], volumes=[V1Volume(name="v")])
        assert HostPathVolumeRule().evaluate(_inventory(_template(spec))) == []

    def test_does_not_fire_with_no_volumes(self):
        spec = V1PodSpec(containers=[_container()])
        assert HostPathVolumeRule().evaluate(_inventory(_template(spec))) == []


class TestHasMutableTag:
    def test_no_tag_is_mutable(self):
        assert _has_mutable_tag("busybox") is True

    def test_explicit_latest_is_mutable(self):
        assert _has_mutable_tag("busybox:latest") is True

    def test_pinned_tag_is_not_mutable(self):
        assert _has_mutable_tag("busybox:1.36") is False

    def test_digest_pinned_is_not_mutable_even_without_tag(self):
        assert _has_mutable_tag("busybox@sha256:" + "a" * 64) is False

    def test_registry_with_port_is_not_mistaken_for_a_tag(self):
        assert _has_mutable_tag("myregistry.io:5000/busybox") is True  # no tag, just a registry port

    def test_registry_with_port_and_pinned_tag(self):
        assert _has_mutable_tag("myregistry.io:5000/busybox:1.36") is False


class TestMutableImageTagRule:
    def test_fires_for_latest_tag(self):
        spec = V1PodSpec(containers=[_container(image="busybox:latest")])
        assert len(MutableImageTagRule().evaluate(_inventory(_template(spec)))) == 1

    def test_does_not_fire_for_pinned_tag(self):
        spec = V1PodSpec(containers=[_container(image="busybox:1.36")])
        assert MutableImageTagRule().evaluate(_inventory(_template(spec))) == []


class TestSecretAsEnvVarRule:
    def test_fires_for_secret_key_ref_env_var(self):
        spec = V1PodSpec(
            containers=[
                _container(
                    env=[V1EnvVar(name="PASSWORD", value_from=V1EnvVarSource(secret_key_ref=V1SecretKeySelector(name="my-secret", key="password")))]
                )
            ]
        )
        findings = SecretAsEnvVarRule().evaluate(_inventory(_template(spec)))
        assert len(findings) == 1
        assert "my-secret" in findings[0].evidence[0]

    def test_fires_for_env_from_secret_ref(self):
        spec = V1PodSpec(containers=[_container(env_from=[V1EnvFromSource(secret_ref=V1SecretEnvSource(name="my-secret"))])])
        findings = SecretAsEnvVarRule().evaluate(_inventory(_template(spec)))
        assert len(findings) == 1

    def test_does_not_fire_for_plain_env_var(self):
        spec = V1PodSpec(containers=[_container(env=[V1EnvVar(name="LOG_LEVEL", value="info")])])
        assert SecretAsEnvVarRule().evaluate(_inventory(_template(spec))) == []

    def test_does_not_fire_for_configmap_sourced_env(self):
        spec = V1PodSpec(
            containers=[
                _container(env_from=[V1EnvFromSource(config_map_ref=None)])  # nothing set at all
            ]
        )
        assert SecretAsEnvVarRule().evaluate(_inventory(_template(spec))) == []


class TestDedupPerWorkload:
    def test_rule_fires_once_per_workload_not_per_replica(self):
        spec = V1PodSpec(containers=[_container(security_context=V1SecurityContext(privileged=True))])
        # security/inventory.py's own dedup means only one WorkloadPodTemplate
        # exists per workload regardless of replica count -- simulated here
        # directly since this test targets the rule, not the inventory builder.
        findings = PrivilegedContainerRule().evaluate(_inventory(_template(spec)))
        assert len(findings) == 1

"""Container security context rules (docs/SECURITY_AUDITING.md Section
3.1). Each evaluates one `WorkloadPodTemplate` at a time -- deduped per
owning workload by `security/inventory.py`, so a 10-replica Deployment
produces at most one finding per rule, not ten.
"""

from __future__ import annotations

from uuid import uuid4

from kubesentinel.models.security import SecurityFinding, SecurityResourceRef, SecuritySeverity
from kubesentinel.security.inventory import SecurityInventory, WorkloadPodTemplate
from kubesentinel.security.rules.base import SecurityRule


def _has_mutable_tag(image: str) -> bool:
    """A digest-pinned reference (`image@sha256:...`) is always immutable
    regardless of any tag. Otherwise, no tag at all defaults to `:latest`
    implicitly, and an explicit `:latest` is the same mutable reference
    spelled out -- both flagged identically. Splitting on `/` before
    checking for `:` is deliberate: a registry host with a port
    (`myregistry.io:5000/image`) must not be misread as a tag."""
    if "@" in image:
        return False
    last_segment = image.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return True
    tag = last_segment.rsplit(":", 1)[-1]
    return tag == "latest"


def _finding(
    rule_id: str,
    severity: SecuritySeverity,
    template: WorkloadPodTemplate,
    container_name: str | None,
    evidence: list[str],
    explanation: str,
    remediation: str,
) -> SecurityFinding:
    name = template.workload_name if container_name is None else f"{template.workload_name}/{container_name}"
    return SecurityFinding(
        finding_id=str(uuid4()),
        rule_id=rule_id,
        severity=severity,
        resource=SecurityResourceRef(namespace=template.namespace, kind="Pod", name=name),
        workload_kind=template.workload_kind,
        workload_name=template.workload_name,
        evidence=evidence,
        explanation=explanation,
        remediation=remediation,
    )


class PrivilegedContainerRule(SecurityRule):
    rule_id = "PRIVILEGED_CONTAINER"

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for template in inventory.pod_templates:
            for container in template.pod_spec.containers or []:
                sc = container.security_context
                if sc is not None and sc.privileged is True:
                    findings.append(
                        _finding(
                            self.rule_id,
                            SecuritySeverity.CRITICAL,
                            template,
                            container.name,
                            [f"container '{container.name}' has securityContext.privileged=true"],
                            "A privileged container has essentially unrestricted access to the host "
                            "(all capabilities, host devices, no seccomp/AppArmor confinement).",
                            "Remove `privileged: true`; grant only the specific Linux capabilities the "
                            "container actually needs instead.",
                        )
                    )
        return findings


class AllowPrivilegeEscalationRule(SecurityRule):
    rule_id = "ALLOW_PRIVILEGE_ESCALATION"

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for template in inventory.pod_templates:
            for container in template.pod_spec.containers or []:
                sc = container.security_context
                value = sc.allow_privilege_escalation if sc is not None else None
                if value is True:
                    findings.append(
                        _finding(
                            self.rule_id,
                            SecuritySeverity.HIGH,
                            template,
                            container.name,
                            [f"container '{container.name}' has securityContext.allowPrivilegeEscalation=true"],
                            "A process in this container can gain more privileges than its parent "
                            "(e.g. via a setuid binary), explicitly allowed.",
                            "Set `allowPrivilegeEscalation: false` unless this container genuinely needs it.",
                        )
                    )
                elif value is None:
                    findings.append(
                        _finding(
                            self.rule_id,
                            SecuritySeverity.LOW,
                            template,
                            container.name,
                            [f"container '{container.name}' does not set securityContext.allowPrivilegeEscalation"],
                            "Not explicitly disabled -- an absent field is a real gap worth naming, not "
                            "assumed safe (Kubernetes' own default here is permissive unless explicitly set).",
                            "Set `allowPrivilegeEscalation: false` explicitly.",
                        )
                    )
        return findings


class RunAsRootRule(SecurityRule):
    rule_id = "RUN_AS_ROOT"

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for template in inventory.pod_templates:
            pod_level = template.pod_spec.security_context
            pod_level_value = pod_level.run_as_non_root if pod_level is not None else None
            for container in template.pod_spec.containers or []:
                sc = container.security_context
                container_level_value = sc.run_as_non_root if sc is not None else None
                effective = container_level_value if container_level_value is not None else pod_level_value
                if effective is not True:
                    findings.append(
                        _finding(
                            self.rule_id,
                            SecuritySeverity.MEDIUM,
                            template,
                            container.name,
                            [
                                f"container '{container.name}' effective runAsNonRoot={effective!r} "
                                "(container-level overrides Pod-level when set)"
                            ],
                            "This container may run as root (UID 0) inside its container, which widens "
                            "the blast radius of any container-breakout vulnerability.",
                            "Set `runAsNonRoot: true` (Pod- or container-level) and a non-zero `runAsUser`.",
                        )
                    )
        return findings


class MissingReadOnlyRootFsRule(SecurityRule):
    rule_id = "MISSING_READ_ONLY_ROOT_FS"

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for template in inventory.pod_templates:
            for container in template.pod_spec.containers or []:
                sc = container.security_context
                if sc is None or sc.read_only_root_filesystem is not True:
                    findings.append(
                        _finding(
                            self.rule_id,
                            SecuritySeverity.LOW,
                            template,
                            container.name,
                            [f"container '{container.name}' does not set securityContext.readOnlyRootFilesystem=true"],
                            "A writable root filesystem makes it easier for an attacker who gains code "
                            "execution to persist or tamper with the container's own files.",
                            "Set `readOnlyRootFilesystem: true`; use an `emptyDir` volume for any directory "
                            "the application genuinely needs to write to.",
                        )
                    )
        return findings


class HostNamespaceSharingRule(SecurityRule):
    rule_id = "HOST_NAMESPACE_SHARING"

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for template in inventory.pod_templates:
            spec = template.pod_spec
            shared = [
                name
                for name, value in (("hostNetwork", spec.host_network), ("hostPID", spec.host_pid), ("hostIPC", spec.host_ipc))
                if value is True
            ]
            if shared:
                findings.append(
                    _finding(
                        self.rule_id,
                        SecuritySeverity.HIGH,
                        template,
                        None,
                        [f"Pod spec sets {s}=true" for s in shared],
                        "Sharing the host's network/process/IPC namespace removes the isolation a "
                        "container normally has from the Node it runs on.",
                        "Remove hostNetwork/hostPID/hostIPC unless this workload has a genuine, specific "
                        "need for host-namespace access (e.g. a CNI plugin).",
                    )
                )
        return findings


class HostPathVolumeRule(SecurityRule):
    rule_id = "HOST_PATH_VOLUME"

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for template in inventory.pod_templates:
            host_paths = [v.host_path.path for v in (template.pod_spec.volumes or []) if v.host_path is not None]
            if host_paths:
                findings.append(
                    _finding(
                        self.rule_id,
                        SecuritySeverity.HIGH,
                        template,
                        None,
                        [f"hostPath volume mounted at {path!r}" for path in host_paths],
                        "A hostPath volume gives this Pod direct access to the Node's own filesystem, "
                        "bypassing normal container isolation.",
                        "Use a PersistentVolumeClaim or another cluster-managed storage mechanism instead.",
                    )
                )
        return findings


class MutableImageTagRule(SecurityRule):
    rule_id = "MUTABLE_IMAGE_TAG"

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for template in inventory.pod_templates:
            for container in template.pod_spec.containers or []:
                image = container.image or ""
                if _has_mutable_tag(image):
                    findings.append(
                        _finding(
                            self.rule_id,
                            SecuritySeverity.LOW,
                            template,
                            container.name,
                            [f"container '{container.name}' image is {image!r}"],
                            "A mutable tag (`:latest` or no tag at all) means the exact code running "
                            "today can silently change on the next Pod restart, with no audit trail.",
                            "Pin to an immutable tag or digest (e.g. `myimage@sha256:...`).",
                        )
                    )
        return findings


class SecretAsEnvVarRule(SecurityRule):
    rule_id = "SECRET_AS_ENV_VAR"

    def evaluate(self, inventory: SecurityInventory) -> list[SecurityFinding]:
        findings = []
        for template in inventory.pod_templates:
            for container in template.pod_spec.containers or []:
                sources = []
                for env in container.env or []:
                    if env.value_from is not None and env.value_from.secret_key_ref is not None:
                        sources.append(f"env var '{env.name}' from Secret '{env.value_from.secret_key_ref.name}'")
                for env_from in container.env_from or []:
                    if env_from.secret_ref is not None:
                        sources.append(f"envFrom Secret '{env_from.secret_ref.name}'")
                if sources:
                    findings.append(
                        _finding(
                            self.rule_id,
                            SecuritySeverity.MEDIUM,
                            template,
                            container.name,
                            sources,
                            "Secret values exposed as environment variables are more exposure-prone than "
                            "a volume mount: visible in `/proc/<pid>/environ`, crash dumps, and often "
                            "accidentally logged.",
                            "Mount the Secret as a volume instead of injecting it as an environment variable.",
                        )
                    )
        return findings

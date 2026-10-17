"""CVE/image vulnerability scanning via a local Trivy binary
(docs/SECURITY_AUDITING.md Section 14) -- the one check in this engine
that shells out to an external scanner rather than reading an
already-typed Kubernetes object directly, because deciding whether an
image has a known CVE genuinely needs a maintained vulnerability
database this project has no intention of building.

Not a `SecurityRule`: a subprocess call is not a pure function over
already-fetched evidence the way every rule in `security/rules/`
is, so this lives in its own module and is orchestrated by
`SecurityService.audit()` directly, the same "orchestration lives in
service.py" split every engine in this project already uses.

Genuinely optional, gated by `Settings.security_enable_image_scanning`
(default False) -- off means zero code here runs and zero subprocess
calls happen. On, but `trivy` missing from PATH, degrades to a
`warnings` entry rather than failing the audit -- the rest of this
engine's native checks are never affected either way.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from uuid import uuid4

from kubesentinel.config import Settings
from kubesentinel.models.security import SecurityFinding, SecurityResourceRef, SecuritySeverity
from kubesentinel.security.inventory import SecurityInventory

logger = logging.getLogger(__name__)

RULE_ID = "IMAGE_HAS_KNOWN_VULNERABILITIES"

_SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
_SEVERITY_MAP = {
    "CRITICAL": SecuritySeverity.CRITICAL,
    "HIGH": SecuritySeverity.HIGH,
    "MEDIUM": SecuritySeverity.MEDIUM,
    "LOW": SecuritySeverity.LOW,
}
_MAX_EXAMPLE_CVE_IDS = 10


def trivy_available() -> bool:
    return shutil.which("trivy") is not None


def _images_to_workloads(inventory: SecurityInventory) -> dict[str, list[tuple[str, str, str]]]:
    """image ref -> every (namespace, workload_kind, workload_name) that
    references it, deduped by image so one Trivy scan per distinct image
    covers every workload using it. `initContainers` are not included --
    the same existing simplification every rule in
    `security/rules/container.py` already makes."""
    images: dict[str, list[tuple[str, str, str]]] = {}
    for template in inventory.pod_templates:
        for container in template.pod_spec.containers or []:
            if not container.image:
                continue
            images.setdefault(container.image, []).append(
                (template.namespace, template.workload_kind, template.workload_name)
            )
    return images


def _run_trivy(image: str, *, severity_floor: str, timeout_seconds: int) -> dict | None:
    try:
        result = subprocess.run(
            ["trivy", "image", "--format", "json", "--quiet", "--scanners", "vuln", "--severity", severity_floor, image],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("trivy scan of %r timed out after %ss", image, timeout_seconds)
        return None
    except OSError as exc:
        logger.warning("could not run trivy for %r: %s", image, exc)
        return None

    if result.returncode != 0:
        logger.warning("trivy scan of %r exited %s: %s", image, result.returncode, result.stderr.strip())
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("trivy output for %r was not valid JSON", image)
        return None


def _findings_for_image(
    image: str, workloads: list[tuple[str, str, str]], scan_data: dict
) -> list[SecurityFinding]:
    vulnerabilities: list[dict] = []
    for scan_result in scan_data.get("Results") or []:
        vulnerabilities.extend(scan_result.get("Vulnerabilities") or [])
    if not vulnerabilities:
        return []

    counts: dict[str, int] = {}
    for vuln in vulnerabilities:
        severity = vuln.get("Severity", "UNKNOWN")
        counts[severity] = counts.get(severity, 0) + 1
    worst = next((s for s in _SEVERITY_ORDER if counts.get(s)), "LOW")
    severity = _SEVERITY_MAP.get(worst, SecuritySeverity.LOW)

    def _rank(vuln: dict) -> int:
        sev = vuln.get("Severity", "LOW")
        return _SEVERITY_ORDER.index(sev) if sev in _SEVERITY_ORDER else len(_SEVERITY_ORDER)

    # The same CVE ID routinely appears more than once (affecting several
    # packages, or reported under both os-pkgs and lang-pkgs) -- deduped
    # here so the example list names distinct CVEs, not the same one
    # repeated; `counts` above stays the raw per-(CVE, package) tally,
    # matching Trivy's own convention.
    example_ids: list[str] = []
    for vuln in sorted(vulnerabilities, key=_rank):
        vuln_id = vuln.get("VulnerabilityID", "?")
        if vuln_id not in example_ids:
            example_ids.append(vuln_id)
        if len(example_ids) >= _MAX_EXAMPLE_CVE_IDS:
            break
    count_summary = ", ".join(f"{counts[s]} {s}" for s in _SEVERITY_ORDER if counts.get(s))

    findings = []
    for namespace, workload_kind, workload_name in workloads:
        findings.append(
            SecurityFinding(
                finding_id=str(uuid4()),
                rule_id=RULE_ID,
                severity=severity,
                resource=SecurityResourceRef(namespace=namespace, kind=workload_kind, name=workload_name),
                workload_kind=workload_kind,
                workload_name=workload_name,
                evidence=[
                    f"image '{image}' has known vulnerabilities: {count_summary}",
                    f"example CVE id(s): {', '.join(example_ids)}",
                ],
                explanation=(
                    "A Trivy scan of this image found known CVEs at or above the configured "
                    "severity floor -- an exploitable vulnerability in a package this image ships."
                ),
                remediation="Rebuild from an updated base image and re-scan, or check each CVE's own fix/upgrade path.",
            )
        )
    return findings


def scan_images(inventory: SecurityInventory, settings: Settings) -> tuple[list[SecurityFinding], list[str]]:
    """Returns (findings, warnings) -- never raises; a scan failure for
    one image is recorded as a warning and skipped, the rest of the
    audit (and every other image) proceeds unaffected."""
    if not trivy_available():
        return [], ["image vulnerability scanning enabled but 'trivy' binary not found on PATH -- skipped"]

    findings: list[SecurityFinding] = []
    warnings: list[str] = []
    for image, workloads in _images_to_workloads(inventory).items():
        scan_data = _run_trivy(
            image,
            severity_floor=settings.security_image_scan_min_severity,
            timeout_seconds=settings.security_image_scan_timeout_seconds,
        )
        if scan_data is None:
            warnings.append(f"could not scan image '{image}' with trivy -- see logs")
            continue
        findings.extend(_findings_for_image(image, workloads, scan_data))
    return findings, warnings

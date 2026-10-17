import json
import subprocess
from unittest.mock import patch

from kubernetes.client import V1Container, V1PodSpec

from kubesentinel.config import Settings
from kubesentinel.security.image_scanning import (
    RULE_ID,
    _findings_for_image,
    _images_to_workloads,
    _run_trivy,
    scan_images,
    trivy_available,
)
from kubesentinel.security.inventory import SecurityInventory, WorkloadPodTemplate

NAMESPACE = "kubesentinel-lab"


def _template(image: str, workload_name="demo") -> WorkloadPodTemplate:
    return WorkloadPodTemplate(
        namespace=NAMESPACE,
        workload_kind="Deployment",
        workload_name=workload_name,
        pod_spec=V1PodSpec(containers=[V1Container(name="app", image=image)]),
    )


def _trivy_result(*vulns: dict) -> dict:
    return {"Results": [{"Target": "app", "Vulnerabilities": list(vulns)}]}


def _vuln(cve="CVE-2024-0001", severity="HIGH") -> dict:
    return {"VulnerabilityID": cve, "Severity": severity, "PkgName": "libfoo"}


class TestImagesToWorkloads:
    def test_dedupes_shared_image_across_workloads(self):
        inventory = SecurityInventory(pod_templates=[_template("nginx:1.25", "a"), _template("nginx:1.25", "b")])

        images = _images_to_workloads(inventory)

        assert list(images.keys()) == ["nginx:1.25"]
        assert len(images["nginx:1.25"]) == 2

    def test_distinct_images_kept_separate(self):
        inventory = SecurityInventory(pod_templates=[_template("nginx:1.25", "a"), _template("redis:7", "b")])

        images = _images_to_workloads(inventory)

        assert set(images.keys()) == {"nginx:1.25", "redis:7"}

    def test_container_with_no_image_skipped(self):
        template = WorkloadPodTemplate(
            namespace=NAMESPACE, workload_kind="Deployment", workload_name="a", pod_spec=V1PodSpec(containers=[V1Container(name="app")])
        )
        inventory = SecurityInventory(pod_templates=[template])

        assert _images_to_workloads(inventory) == {}


class TestFindingsForImage:
    def test_no_vulnerabilities_produces_no_findings(self):
        findings = _findings_for_image("nginx:1.25", [(NAMESPACE, "Deployment", "demo")], {"Results": []})
        assert findings == []

    def test_one_finding_per_referencing_workload(self):
        scan_data = _trivy_result(_vuln())
        findings = _findings_for_image(
            "nginx:1.25", [(NAMESPACE, "Deployment", "a"), (NAMESPACE, "Deployment", "b")], scan_data
        )
        assert len(findings) == 2
        assert {f.workload_name for f in findings} == {"a", "b"}
        assert all(f.rule_id == RULE_ID for f in findings)

    def test_severity_is_the_worst_cve_found(self):
        scan_data = _trivy_result(_vuln(severity="MEDIUM"), _vuln(cve="CVE-2024-0002", severity="CRITICAL"))
        findings = _findings_for_image("nginx:1.25", [(NAMESPACE, "Deployment", "a")], scan_data)
        assert findings[0].severity.value == "CRITICAL"

    def test_evidence_names_counts_and_example_cve_ids(self):
        scan_data = _trivy_result(_vuln(), _vuln(cve="CVE-2024-0002"))
        findings = _findings_for_image("nginx:1.25", [(NAMESPACE, "Deployment", "a")], scan_data)
        evidence_text = " ".join(findings[0].evidence)
        assert "2 HIGH" in evidence_text
        assert "CVE-2024-0001" in evidence_text
        assert "CVE-2024-0002" in evidence_text

    def test_example_cve_ids_capped(self):
        vulns = [_vuln(cve=f"CVE-2024-{i:04d}") for i in range(20)]
        scan_data = _trivy_result(*vulns)
        findings = _findings_for_image("nginx:1.25", [(NAMESPACE, "Deployment", "a")], scan_data)
        example_line = next(e for e in findings[0].evidence if "example CVE" in e)
        assert example_line.count("CVE-") == 10

    def test_example_cve_ids_deduped_across_repeated_packages(self):
        # The same CVE ID genuinely appears more than once in real Trivy
        # output -- once per affected package. The example list should
        # name distinct CVEs, not the same one repeated.
        vulns = [_vuln(cve="CVE-2024-0001", severity="CRITICAL")] * 5 + [_vuln(cve="CVE-2024-0002", severity="HIGH")]
        scan_data = _trivy_result(*vulns)
        findings = _findings_for_image("nginx:1.25", [(NAMESPACE, "Deployment", "a")], scan_data)
        example_line = next(e for e in findings[0].evidence if "example CVE" in e)
        assert example_line.count("CVE-2024-0001") == 1
        assert "CVE-2024-0002" in example_line


class TestRunTrivy:
    def test_parses_valid_json_output(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps({"Results": []}), stderr="")
            result = _run_trivy("nginx:1.25", severity_floor="HIGH,CRITICAL", timeout_seconds=10)
        assert result == {"Results": []}

    def test_nonzero_exit_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="image not found")
            result = _run_trivy("bogus:1.0", severity_floor="HIGH,CRITICAL", timeout_seconds=10)
        assert result is None

    def test_timeout_returns_none(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="trivy", timeout=10)):
            result = _run_trivy("nginx:1.25", severity_floor="HIGH,CRITICAL", timeout_seconds=10)
        assert result is None

    def test_invalid_json_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
            result = _run_trivy("nginx:1.25", severity_floor="HIGH,CRITICAL", timeout_seconds=10)
        assert result is None

    def test_missing_binary_returns_none(self):
        with patch("subprocess.run", side_effect=OSError("no such file")):
            result = _run_trivy("nginx:1.25", severity_floor="HIGH,CRITICAL", timeout_seconds=10)
        assert result is None

    def test_uses_list_args_never_a_shell_string(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
            _run_trivy("nginx:1.25", severity_floor="HIGH,CRITICAL", timeout_seconds=10)
        args, kwargs = mock_run.call_args
        assert isinstance(args[0], list)
        assert "nginx:1.25" in args[0]
        assert kwargs.get("timeout") == 10


class TestScanImages:
    def test_trivy_not_available_returns_warning_and_no_findings(self):
        inventory = SecurityInventory(pod_templates=[_template("nginx:1.25")])
        with patch("kubesentinel.security.image_scanning.trivy_available", return_value=False):
            findings, warnings = scan_images(inventory, Settings())
        assert findings == []
        assert len(warnings) == 1
        assert "trivy" in warnings[0].lower()

    def test_scan_failure_for_one_image_recorded_as_warning_others_still_scanned(self):
        inventory = SecurityInventory(pod_templates=[_template("bad:1.0", "a"), _template("good:1.0", "b")])

        def fake_run_trivy(image, **kwargs):
            return None if image == "bad:1.0" else {"Results": []}

        with (
            patch("kubesentinel.security.image_scanning.trivy_available", return_value=True),
            patch("kubesentinel.security.image_scanning._run_trivy", side_effect=fake_run_trivy),
        ):
            findings, warnings = scan_images(inventory, Settings())

        assert findings == []
        assert len(warnings) == 1
        assert "bad:1.0" in warnings[0]

    def test_findings_produced_when_vulnerabilities_found(self):
        inventory = SecurityInventory(pod_templates=[_template("nginx:1.25")])
        with (
            patch("kubesentinel.security.image_scanning.trivy_available", return_value=True),
            patch("kubesentinel.security.image_scanning._run_trivy", return_value=_trivy_result(_vuln())),
        ):
            findings, warnings = scan_images(inventory, Settings())
        assert len(findings) == 1
        assert warnings == []


def test_trivy_available_reflects_path():
    with patch("shutil.which", return_value=None):
        assert trivy_available() is False
    with patch("shutil.which", return_value="/usr/local/bin/trivy"):
        assert trivy_available() is True

from kubesentinel.models.evidence import EvidenceKind
from kubesentinel.observer.inspector import pod_evidence, resource_ref_from_pod
from tests.unit.factories import make_pod, running_container_status, waiting_container_status


def test_pod_evidence_includes_phase():
    pod = make_pod(phase="Running", container_statuses=[running_container_status()])
    evidence = pod_evidence(pod)
    phase_evidence = [e for e in evidence if e.kind == EvidenceKind.POD_STATUS]
    assert len(phase_evidence) == 1
    assert phase_evidence[0].data["phase"] == "Running"


def test_pod_evidence_captures_waiting_reason():
    cs = waiting_container_status(reason="ImagePullBackOff", message="repo not found")
    pod = make_pod(phase="Pending", container_statuses=[cs])
    evidence = pod_evidence(pod)

    waiting = [e for e in evidence if e.data.get("waiting_reason") == "ImagePullBackOff"]
    assert len(waiting) == 1
    assert "repo not found" in waiting[0].data["waiting_message"]


def test_pod_evidence_captures_last_termination():
    cs = waiting_container_status(
        reason="CrashLoopBackOff", last_terminated_reason="OOMKilled", last_exit_code=137
    )
    pod = make_pod(container_statuses=[cs])
    evidence = pod_evidence(pod)

    terminations = [e for e in evidence if e.kind == EvidenceKind.CONTAINER_TERMINATION]
    assert len(terminations) == 1
    assert terminations[0].data["last_terminated_reason"] == "OOMKilled"
    assert terminations[0].data["last_exit_code"] == 137


def test_pod_evidence_handles_missing_status():
    pod = make_pod()
    pod.status = None
    assert pod_evidence(pod) == []


def test_resource_ref_from_pod_captures_owner():
    pod = make_pod(owner=("ReplicaSet", "web-abc123", "rs-uid-1"))
    ref = resource_ref_from_pod(pod)
    assert ref.owner_kind == "ReplicaSet"
    assert ref.owner_name == "web-abc123"
    assert ref.owner_uid == "rs-uid-1"


def test_resource_ref_from_pod_without_owner():
    pod = make_pod(owner=None)
    ref = resource_ref_from_pod(pod)
    assert ref.owner_kind is None

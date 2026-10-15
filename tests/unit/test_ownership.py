from kubernetes.client import V1ObjectMeta, V1OwnerReference, V1Pod, V1PodSpec

from kubesentinel.models.optimization import WorkloadKind
from kubesentinel.optimization.ownership import resolve_pod_owner
from tests.unit.factories import make_pod, make_replica_set


def test_standalone_pod_with_no_owner_resolves_to_pod_kind():
    pod = make_pod(name="standalone", uid="pod-uid-1", owner=None)

    resolved = resolve_pod_owner(pod, replicasets_by_uid={})

    assert resolved.kind == WorkloadKind.POD
    assert resolved.name == "standalone"
    assert resolved.uid == "pod-uid-1"


def test_pod_owned_by_replicaset_resolves_through_to_deployment():
    replica_set = make_replica_set(name="my-deploy-abc123", owner_uid="deploy-uid-1")
    pod = make_pod(owner=("ReplicaSet", "my-deploy-abc123", "rs-uid-1"))

    resolved = resolve_pod_owner(pod, replicasets_by_uid={"rs-uid-1": replica_set})

    assert resolved.kind == WorkloadKind.DEPLOYMENT
    assert resolved.name == "test-deployment"  # make_replica_set's own Deployment owner name
    assert resolved.uid == "deploy-uid-1"


def test_pod_owned_by_statefulset_resolves_directly_no_chain_walk():
    pod = make_pod(owner=("StatefulSet", "my-statefulset", "sts-uid-1"))

    resolved = resolve_pod_owner(pod, replicasets_by_uid={})

    assert resolved.kind == WorkloadKind.STATEFULSET
    assert resolved.name == "my-statefulset"
    assert resolved.uid == "sts-uid-1"


def test_pod_owned_by_daemonset_resolves_directly():
    pod = make_pod(owner=("DaemonSet", "my-daemonset", "ds-uid-1"))

    resolved = resolve_pod_owner(pod, replicasets_by_uid={})

    assert resolved.kind == WorkloadKind.DAEMONSET
    assert resolved.name == "my-daemonset"


def test_pod_owned_by_job_resolves_directly():
    pod = make_pod(owner=("Job", "my-job", "job-uid-1"))

    resolved = resolve_pod_owner(pod, replicasets_by_uid={})

    assert resolved.kind == WorkloadKind.JOB
    assert resolved.name == "my-job"


def test_pod_owned_by_replicaset_missing_from_map_resolves_to_none():
    # The Pod names a ReplicaSet that wasn't in the fetched set -- a race
    # during collection, or cluster drift between the two list calls. Must
    # not guess; the caller decides what to do with an unresolved Pod.
    pod = make_pod(owner=("ReplicaSet", "orphaned-rs", "rs-uid-missing"))

    resolved = resolve_pod_owner(pod, replicasets_by_uid={})

    assert resolved is None


def test_replicaset_with_no_deployment_owner_resolves_to_none():
    # A ReplicaSet created directly (not via a Deployment) has no recognized
    # further parent in this project's model.
    bare_replica_set = make_replica_set(name="bare-rs", owned=False)
    pod = make_pod(owner=("ReplicaSet", "bare-rs", "rs-uid-bare"))

    resolved = resolve_pod_owner(pod, replicasets_by_uid={"rs-uid-bare": bare_replica_set})

    assert resolved is None


def test_pod_owned_by_unrecognized_controller_kind_resolves_to_none():
    # A CRD-based operator (Argo Rollout, etc.) -- not silently treated as
    # standalone, which would misrepresent a managed workload as an orphan.
    pod = make_pod(owner=("Rollout", "my-rollout", "rollout-uid-1"))

    resolved = resolve_pod_owner(pod, replicasets_by_uid={})

    assert resolved is None


def test_prefers_owner_reference_marked_controller_true_over_others():
    pod = V1Pod(
        metadata=V1ObjectMeta(
            name="multi-owner-pod",
            namespace="kubesentinel-lab",
            uid="pod-uid-multi",
            owner_references=[
                V1OwnerReference(
                    api_version="v1", kind="ConfigMap", name="unrelated", uid="cm-uid", controller=False
                ),
                V1OwnerReference(
                    api_version="apps/v1", kind="StatefulSet", name="real-owner", uid="sts-uid-real", controller=True
                ),
            ],
        ),
        spec=V1PodSpec(containers=[]),
    )

    resolved = resolve_pod_owner(pod, replicasets_by_uid={})

    assert resolved.kind == WorkloadKind.STATEFULSET
    assert resolved.name == "real-owner"


def test_pod_with_no_metadata_owner_references_field_is_standalone():
    pod = V1Pod(
        metadata=V1ObjectMeta(name="no-refs", namespace="kubesentinel-lab", uid="pod-uid-none"),
        spec=V1PodSpec(containers=[]),
    )

    resolved = resolve_pod_owner(pod, replicasets_by_uid={})

    assert resolved.kind == WorkloadKind.POD

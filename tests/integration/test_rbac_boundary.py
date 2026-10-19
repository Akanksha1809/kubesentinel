"""Live RBAC boundary tests against the real kind cluster.

Confirms what RBAC_AND_THREAT_MODEL.md claims -- least privilege, scoped to
exactly what the observer and remediation executor actually do -- is genuinely
enforced by the API server for the real `kubesentinel` ServiceAccount, not just
true of the YAML read statically. Uses two complementary techniques:

1. `kubectl auth can-i --as=...`: the authoritative RBAC decision (a real
   SelfSubjectAccessReview), the same command used to manually confirm this
   boundary in Milestone 4 (see TODO.md #14) -- now automated.
2. A real API call through a client authenticated with that ServiceAccount's own
   minted token (`kubesentinel_sa_clients`), to prove the token path itself
   works end-to-end, not only that `can-i` would say so.

No test here performs a real mutation -- every "should be denied" check is
something the API server rejects before it could do anything, and every
"should be allowed" check is a read.
"""

from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from tests.conftest import LAB_NAMESPACE, REMEDIATION_TEST_DEPLOYMENT, kubectl_can_i

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")


# --- Observer Role (rbac/role.yaml): read-only, lab namespace only ---


def test_can_list_pods_in_lab_namespace(kubesentinel_sa_clients):
    core, _, _, _ = kubesentinel_sa_clients
    pods = core.list_namespaced_pod(namespace=LAB_NAMESPACE, limit=1)
    assert pods is not None


def test_can_list_events_in_lab_namespace(kubesentinel_sa_clients):
    core, _, _, _ = kubesentinel_sa_clients
    events = core.list_namespaced_event(namespace=LAB_NAMESPACE, limit=1)
    assert events is not None


def test_can_list_deployments_in_lab_namespace(kubesentinel_sa_clients):
    _, apps, _, _ = kubesentinel_sa_clients
    deployments = apps.list_namespaced_deployment(namespace=LAB_NAMESPACE, limit=1)
    assert deployments is not None


def test_cannot_read_secrets_anywhere():
    assert kubectl_can_i("get", "secrets", namespace=LAB_NAMESPACE) is False
    assert kubectl_can_i("list", "secrets") is False  # cluster-wide


def test_cannot_list_pods_outside_lab_namespace(kubesentinel_sa_clients):
    core, _, _, _ = kubesentinel_sa_clients
    with pytest.raises(ApiException) as exc_info:
        core.list_namespaced_pod(namespace="kube-system")
    assert exc_info.value.status == 403


def test_can_read_nodes_cluster_scoped(kubesentinel_sa_clients):
    # The one deliberate cluster-scoped exception (rbac/clusterrole.yaml) --
    # Node has no namespace, so this can't be expressed as a namespaced Role.
    core, _, _, _ = kubesentinel_sa_clients
    nodes = core.list_node(limit=1)
    assert nodes is not None


def test_cannot_delete_nodes():
    assert kubectl_can_i("delete", "nodes") is False


def test_cannot_delete_pods_directly():
    # The executor restarts a Deployment via its ReplicaSet, never by deleting a
    # Pod directly -- this Role grants no delete verb on Pods at all.
    assert kubectl_can_i("delete", "pods", namespace=LAB_NAMESPACE) is False


# --- Remediator Role (rbac/remediation-role.yaml): narrow write access ---


def test_can_update_deployments_in_lab_namespace():
    # replace_namespaced_deployment (rollback) issues an HTTP PUT -> the
    # "update" verb, confirmed distinct from "patch" live in Milestone 4.
    assert kubectl_can_i("update", "deployments", namespace=LAB_NAMESPACE) is True


def test_can_patch_deployments_in_lab_namespace():
    # patch_namespaced_deployment (restart's template annotation) -> "patch".
    assert kubectl_can_i("patch", "deployments", namespace=LAB_NAMESPACE) is True


def test_can_patch_deployment_scale_subresource():
    assert kubectl_can_i("patch", "deployments", namespace=LAB_NAMESPACE, subresource="scale") is True


def test_can_list_replicasets_for_rollback_revision_lookup():
    assert kubectl_can_i("list", "replicasets", namespace=LAB_NAMESPACE) is True


def test_cannot_delete_deployments(kubesentinel_sa_clients):
    _, apps, _, _ = kubesentinel_sa_clients
    with pytest.raises(ApiException) as exc_info:
        apps.delete_namespaced_deployment(name=REMEDIATION_TEST_DEPLOYMENT, namespace=LAB_NAMESPACE)
    assert exc_info.value.status == 403


def test_cannot_patch_replicasets():
    # Read-only on ReplicaSets -- only used to look up revision history, never
    # mutated directly.
    assert kubectl_can_i("patch", "replicasets", namespace=LAB_NAMESPACE) is False


def test_cannot_update_or_patch_deployments_outside_lab_namespace():
    assert kubectl_can_i("update", "deployments", namespace="kube-system") is False
    assert kubectl_can_i("patch", "deployments", namespace="kube-system") is False


def test_cannot_mutate_pvcs_or_services():
    # Milestone 4's executor never mutates PVCs/Services/Nodes/Events -- only
    # Deployments (+ its own ReplicaSets, read-only).
    assert kubectl_can_i("patch", "persistentvolumeclaims", namespace=LAB_NAMESPACE) is False
    assert kubectl_can_i("patch", "services", namespace=LAB_NAMESPACE) is False


# --- Optimizer Role (rbac/optimization-role.yaml): read-only, lab namespace only ---


def test_can_list_statefulsets_daemonsets_jobs_in_lab_namespace(kubesentinel_sa_clients):
    _, apps, batch, _ = kubesentinel_sa_clients
    assert apps.list_namespaced_stateful_set(namespace=LAB_NAMESPACE, limit=1) is not None
    assert apps.list_namespaced_daemon_set(namespace=LAB_NAMESPACE, limit=1) is not None
    assert batch.list_namespaced_job(namespace=LAB_NAMESPACE, limit=1) is not None


def test_can_get_pod_metrics_via_real_authenticated_call(kubesentinel_sa_clients):
    # Deliberately NOT asserted via kubectl_can_i: found live that `can-i`
    # gives a wrong answer for this aggregated API (see
    # KUBERNETES_CONCEPTS.md) -- a real request through a real client
    # authenticated as this exact ServiceAccount is the only trustworthy
    # check. 503 (metrics-server installed but unreachable, a real
    # environmental state this project has hit live under shared-VM
    # contention) and 404 (metrics-server not installed at all -- this
    # exact lab's own permanent state, found live while adding the sibling
    # Node-metrics check below: the aggregated API path itself isn't being
    # served, so the main apiserver's own routing returns a plain 404, not
    # a 503 from the aggregation layer) are both "not an RBAC question" --
    # neither is a 403, so neither says anything about this ServiceAccount's
    # own grant.
    _, _, _, custom_objects = kubesentinel_sa_clients
    try:
        result = custom_objects.list_namespaced_custom_object(
            group="metrics.k8s.io", version="v1beta1", namespace=LAB_NAMESPACE, plural="pods"
        )
    except ApiException as exc:
        if exc.status in (404, 503):
            pytest.skip(f"metrics-server unavailable right now (not an RBAC question): {exc}")
        if exc.status == 403:
            pytest.fail(f"expected RBAC to allow this, got 403: {exc}")
        raise
    assert result is not None


# --- Node-metrics ClusterRole (rbac/optimization-clusterrole.yaml): read-only, cluster-scoped ---


def test_can_get_node_metrics_via_real_authenticated_call(kubesentinel_sa_clients):
    # Same real-authenticated-call technique as the pod-metrics test above,
    # for the same reason: `kubectl auth can-i` is unreliable for this
    # aggregated API when metrics-server isn't registered at all --
    # confirmed live (`kubectl auth can-i list nodes.metrics.k8s.io`
    # itself reports the resource type isn't recognized, not a RBAC
    # decision). `metrics.k8s.io/nodes` is cluster-scoped, so this call
    # takes no namespace at all, unlike the Pod-metrics sibling above.
    _, _, _, custom_objects = kubesentinel_sa_clients
    try:
        result = custom_objects.list_cluster_custom_object(group="metrics.k8s.io", version="v1beta1", plural="nodes")
    except ApiException as exc:
        if exc.status in (404, 503):
            pytest.skip(f"metrics-server unavailable right now (not an RBAC question): {exc}")
        if exc.status == 403:
            pytest.fail(f"expected RBAC to allow this, got 403: {exc}")
        raise
    assert result is not None


def test_cannot_patch_statefulsets_daemonsets_jobs_or_replicasets():
    # Read-only Role: optimization/inventory.py only ever lists these, never
    # mutates any of them.
    assert kubectl_can_i("patch", "statefulsets", namespace=LAB_NAMESPACE) is False
    assert kubectl_can_i("patch", "daemonsets", namespace=LAB_NAMESPACE) is False
    assert kubectl_can_i("patch", "jobs", namespace=LAB_NAMESPACE) is False


def test_cannot_list_statefulsets_outside_lab_namespace():
    assert kubectl_can_i("list", "statefulsets", namespace="kube-system") is False


# --- Scheduling advisor Role (rbac/scheduling-role.yaml): read-only, lab namespace only ---


def test_can_list_resourcequotas_in_lab_namespace(kubesentinel_sa_clients):
    core, _, _, _ = kubesentinel_sa_clients
    assert core.list_namespaced_resource_quota(namespace=LAB_NAMESPACE, limit=1) is not None


def test_cannot_patch_resourcequotas():
    # Read-only Role: scheduling/analyzer.py only ever reads ResourceQuota
    # status to check a hypothetical spec against it, never mutates one.
    assert kubectl_can_i("patch", "resourcequotas", namespace=LAB_NAMESPACE) is False


def test_cannot_list_resourcequotas_outside_lab_namespace():
    assert kubectl_can_i("list", "resourcequotas", namespace="kube-system") is False


# --- Network advisor Role (rbac/network-role.yaml): read-only, lab namespace only ---


def test_can_list_services_and_networkpolicies_in_lab_namespace(kubesentinel_sa_clients):
    core, _, _, _ = kubesentinel_sa_clients
    assert core.list_namespaced_service(namespace=LAB_NAMESPACE, limit=1) is not None
    assert kubectl_can_i("list", "networkpolicies.networking.k8s.io", namespace=LAB_NAMESPACE) is True


def test_cannot_patch_services_or_networkpolicies():
    # Read-only Role: network/service_inventory.py and policy_inventory.py
    # only ever list/get these, never mutate either.
    assert kubectl_can_i("patch", "services", namespace=LAB_NAMESPACE) is False
    assert kubectl_can_i("patch", "networkpolicies.networking.k8s.io", namespace=LAB_NAMESPACE) is False


def test_cannot_list_services_outside_lab_namespace():
    assert kubectl_can_i("list", "services", namespace="kube-system") is False


# --- Network advisor's kube-system exception (rbac/network-dns-role.yaml):
# --- read-only, scoped to exactly coredns/kube-dns by name ---


def test_can_read_coredns_and_kube_dns_by_name_in_kube_system():
    assert kubectl_can_i("get", "deployments.apps", namespace="kube-system", resource_name="coredns") is True
    assert kubectl_can_i("get", "services", namespace="kube-system", resource_name="kube-dns") is True
    assert kubectl_can_i("list", "endpointslices.discovery.k8s.io", namespace="kube-system") is True


def test_cannot_read_other_kube_system_resources_by_name():
    # The grant is scoped to exactly "coredns"/"kube-dns" via resourceNames
    # -- confirms it doesn't widen into "any Deployment/Service in
    # kube-system," which `kubectl auth can-i` without a resource name would
    # incorrectly report as "no" either way (see kubectl_can_i's docstring),
    # so this checks a *different* real resource by name instead.
    assert (
        kubectl_can_i("get", "deployments.apps", namespace="kube-system", resource_name="kube-dns-autoscaler")
        is False
    )


def test_cannot_list_pods_or_secrets_in_kube_system():
    # The kube-system exception grants nothing beyond the two named
    # resources plus EndpointSlice listing -- no Pod/Secret access at all.
    assert kubectl_can_i("list", "pods", namespace="kube-system") is False
    assert kubectl_can_i("list", "secrets", namespace="kube-system") is False


def test_cannot_patch_coredns_or_kube_dns():
    assert kubectl_can_i("patch", "deployments.apps", namespace="kube-system", resource_name="coredns") is False
    assert kubectl_can_i("patch", "services", namespace="kube-system", resource_name="kube-dns") is False


# --- Capacity advisor Role (rbac/capacity-role.yaml): read-only, lab namespace only ---


def test_can_list_and_get_hpas_in_lab_namespace():
    assert kubectl_can_i("list", "horizontalpodautoscalers.autoscaling", namespace=LAB_NAMESPACE) is True
    assert kubectl_can_i("get", "horizontalpodautoscalers.autoscaling", namespace=LAB_NAMESPACE) is True


def test_cannot_patch_hpas():
    # Read-only Role: optimization/capacity.py only ever lists HPAs, never
    # mutates minReplicas/maxReplicas or anything else on one.
    assert kubectl_can_i("patch", "horizontalpodautoscalers.autoscaling", namespace=LAB_NAMESPACE) is False


def test_cannot_list_hpas_outside_lab_namespace():
    assert kubectl_can_i("list", "horizontalpodautoscalers.autoscaling", namespace="kube-system") is False


# --- Security auditor ClusterRole (rbac/security-clusterrole.yaml): read-only, cluster-scoped ---


def test_can_list_and_get_clusterroles_and_clusterrolebindings():
    assert kubectl_can_i("list", "clusterroles.rbac.authorization.k8s.io") is True
    assert kubectl_can_i("list", "clusterrolebindings.rbac.authorization.k8s.io") is True
    assert kubectl_can_i("get", "clusterroles.rbac.authorization.k8s.io") is True


def test_cannot_patch_clusterroles_or_clusterrolebindings():
    # Read-only ClusterRole: security/inventory.py only ever lists these,
    # never mutates any Role/binding anywhere.
    assert kubectl_can_i("patch", "clusterroles.rbac.authorization.k8s.io") is False
    assert kubectl_can_i("patch", "clusterrolebindings.rbac.authorization.k8s.io") is False


# --- Security auditor Role (rbac/security-role.yaml): read-only, lab namespace only ---


def test_can_list_roles_rolebindings_and_pdbs_in_lab_namespace():
    assert kubectl_can_i("list", "roles.rbac.authorization.k8s.io", namespace=LAB_NAMESPACE) is True
    assert kubectl_can_i("list", "rolebindings.rbac.authorization.k8s.io", namespace=LAB_NAMESPACE) is True
    assert kubectl_can_i("list", "poddisruptionbudgets.policy", namespace=LAB_NAMESPACE) is True


def test_cannot_patch_roles_rolebindings_or_pdbs():
    assert kubectl_can_i("patch", "roles.rbac.authorization.k8s.io", namespace=LAB_NAMESPACE) is False
    assert kubectl_can_i("patch", "rolebindings.rbac.authorization.k8s.io", namespace=LAB_NAMESPACE) is False
    assert kubectl_can_i("patch", "poddisruptionbudgets.policy", namespace=LAB_NAMESPACE) is False


def test_cannot_list_roles_outside_lab_namespace():
    assert kubectl_can_i("list", "roles.rbac.authorization.k8s.io", namespace="kube-system") is False

from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from kubesentinel.security.inventory import build_security_inventory
from tests.unit.factories import make_pod

NAMESPACE = "kubesentinel-lab"


def _items(objs):
    return SimpleNamespace(items=objs)


def _clients(pods=(), deployments=(), stateful_sets=(), cluster_role_bindings=(), cluster_roles=(), fail=None):
    clients = MagicMock()
    clients.core.list_namespaced_pod.return_value = _items(list(pods))
    clients.core.list_pod_for_all_namespaces.return_value = _items(list(pods))
    clients.apps.list_namespaced_replica_set.return_value = _items([])
    clients.apps.list_replica_set_for_all_namespaces.return_value = _items([])
    clients.apps.list_namespaced_deployment.return_value = _items(list(deployments))
    clients.apps.list_deployment_for_all_namespaces.return_value = _items(list(deployments))
    clients.apps.list_namespaced_stateful_set.return_value = _items(list(stateful_sets))
    clients.apps.list_stateful_set_for_all_namespaces.return_value = _items(list(stateful_sets))
    clients.rbac_authorization.list_cluster_role_binding.return_value = _items(list(cluster_role_bindings))
    clients.rbac_authorization.list_cluster_role.return_value = _items(list(cluster_roles))
    clients.rbac_authorization.list_role_binding_for_all_namespaces.return_value = _items([])
    clients.rbac_authorization.list_role_for_all_namespaces.return_value = _items([])
    clients.rbac_authorization.list_namespaced_role_binding.return_value = _items([])
    clients.rbac_authorization.list_namespaced_role.return_value = _items([])
    clients.networking.list_namespaced_network_policy.return_value = _items([])
    clients.policy.list_pod_disruption_budget_for_all_namespaces.return_value = _items([])
    clients.policy.list_namespaced_pod_disruption_budget.return_value = _items([])

    if fail:
        parts = fail.split(".")
        target = clients
        for part in parts[:-1]:
            target = getattr(target, part)
        getattr(target, parts[-1]).side_effect = ApiException(status=403, reason="Forbidden")

    return clients


def _pod_with_owner(name, uid, owner):
    pod = make_pod(name=name, namespace=NAMESPACE, uid=uid, owner=owner)
    return pod


class TestBuildSecurityInventory:
    def test_dedup_with_resolvable_owner(self):
        from kubernetes.client import V1ObjectMeta, V1OwnerReference, V1ReplicaSet, V1ReplicaSetSpec

        replica_set = V1ReplicaSet(
            metadata=V1ObjectMeta(
                name="demo-rs",
                namespace=NAMESPACE,
                uid="rs-uid-1",
                owner_references=[V1OwnerReference(api_version="apps/v1", kind="Deployment", name="demo", uid="dep-uid-1", controller=True)],
            ),
            spec=V1ReplicaSetSpec(selector={}),
        )
        pods = [
            _pod_with_owner("pod-a", "uid-a", ("ReplicaSet", "demo-rs", "rs-uid-1")),
            _pod_with_owner("pod-b", "uid-b", ("ReplicaSet", "demo-rs", "rs-uid-1")),
        ]
        clients = _clients(pods=pods)
        clients.apps.list_namespaced_replica_set.return_value = _items([replica_set])

        inventory = build_security_inventory(clients, namespaces=[NAMESPACE])

        assert len(inventory.pod_templates) == 1
        assert inventory.pod_templates[0].workload_name == "demo"

    def test_namespaces_with_pods_tracked(self):
        clients = _clients(pods=[make_pod(name="p", namespace=NAMESPACE, owner=None)])

        inventory = build_security_inventory(clients, namespaces=[NAMESPACE])

        assert NAMESPACE in inventory.namespaces_with_pods

    def test_cluster_role_binding_read_failure_recorded_as_warning(self):
        clients = _clients(fail="rbac_authorization.list_cluster_role_binding")

        inventory = build_security_inventory(clients, namespaces=[NAMESPACE])

        assert inventory.cluster_role_bindings == []
        assert any("ClusterRoleBindings" in w for w in inventory.warnings)

    def test_other_reads_unaffected_by_one_failure(self):
        clients = _clients(
            cluster_roles=[],
            fail="rbac_authorization.list_cluster_role_binding",
        )
        clients.apps.list_namespaced_deployment.return_value = _items([])

        inventory = build_security_inventory(clients, namespaces=[NAMESPACE])

        # cluster_roles read should have happened fine despite the other failure
        assert inventory.cluster_roles == []
        assert len(inventory.warnings) == 1

    def test_all_namespaces_when_none_given(self):
        clients = _clients(pods=[make_pod(name="p", namespace=NAMESPACE, owner=None)])

        build_security_inventory(clients, namespaces=None)

        clients.core.list_pod_for_all_namespaces.assert_called_once()
        clients.core.list_namespaced_pod.assert_not_called()

    def test_per_namespace_when_given(self):
        clients = _clients(pods=[make_pod(name="p", namespace=NAMESPACE, owner=None)])

        build_security_inventory(clients, namespaces=[NAMESPACE])

        clients.core.list_namespaced_pod.assert_called_once_with(namespace=NAMESPACE)
        clients.core.list_pod_for_all_namespaces.assert_not_called()

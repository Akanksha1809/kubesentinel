"""Shared fixtures for tests/integration and tests/e2e: both require a real,
reachable Kubernetes cluster (this project's `kind-kubesentinel` in practice).

`tests/unit/` never sees this file -- `./scripts/test.sh` (no `--all`) runs
`pytest tests/unit` directly, and pytest only walks conftest.py files from the
rootdir down to the paths actually collected, never sideways into sibling
directories. Only `pytest tests` (i.e. `./scripts/test.sh --all`) collects
anything under here, and only then does this file's fixtures matter.

Every fixture here skips (not fails) when its precondition isn't met -- a live
cluster being unreachable, `kubectl` being absent, the lab namespace not
existing -- so `--all` stays runnable (with skips reported honestly) on a
machine that hasn't run `./lab/create-cluster.sh` and `./scripts/demo.sh`,
rather than erroring out entirely.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator

import pytest
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from kubesentinel.config import Settings
from kubesentinel.observer.client import ClusterClients, build_clients

LAB_NAMESPACE = "kubesentinel-lab"
REMEDIATION_TEST_DEPLOYMENT = "remediation-test"


@pytest.fixture(scope="session")
def live_cluster_clients() -> ClusterClients:
    """Real `ClusterClients` against whatever kubeconfig/context is active --
    the same construction path `api/app.py`'s lifespan uses, not a fake."""
    try:
        clients = build_clients(Settings())
        clients.core.list_namespace(limit=1, _request_timeout=5)
    except Exception as exc:  # noqa: BLE001 - any failure here means "skip the suite"
        pytest.skip(f"no live Kubernetes cluster reachable: {exc}")
    return clients


@pytest.fixture(scope="session")
def lab_namespace_ready(live_cluster_clients: ClusterClients) -> None:
    """Skips unless both the lab namespace and its remediation-test fixture
    Deployment exist -- most tests in this suite need the lab already set up via
    `./lab/create-cluster.sh` (+ at least the `remediation-test` fixture, and for
    the observer-detection tests, the `crashloop`/`oom` scenarios)."""
    try:
        live_cluster_clients.core.read_namespace(LAB_NAMESPACE)
    except ApiException as exc:
        pytest.skip(f"'{LAB_NAMESPACE}' namespace not present -- run ./lab/create-cluster.sh: {exc}")


@pytest.fixture(scope="session")
def kubesentinel_sa_token(live_cluster_clients: ClusterClients) -> str:
    """A real bearer token for the `kubesentinel` ServiceAccount, minted with
    `kubectl create token` -- not fabricated. RBAC tests authenticate as this
    exact identity, the same one rbac/*.yaml binds, rather than only reading the
    YAML and trusting it matches runtime behavior."""
    try:
        result = subprocess.run(
            ["kubectl", "create", "token", "kubesentinel", "-n", LAB_NAMESPACE, "--duration=10m"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except FileNotFoundError:
        pytest.skip("kubectl not found on PATH")
    except subprocess.TimeoutExpired:
        pytest.skip("kubectl create token timed out")
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"could not mint a token for the kubesentinel ServiceAccount: {exc.stderr}")
    return result.stdout.strip()


@pytest.fixture(scope="session")
def kubesentinel_sa_clients(
    live_cluster_clients: ClusterClients, kubesentinel_sa_token: str
) -> tuple[k8s_client.CoreV1Api, k8s_client.AppsV1Api, k8s_client.BatchV1Api, k8s_client.CustomObjectsApi]:
    """API clients authenticated as the real `kubesentinel` ServiceAccount --
    not the admin kubeconfig every other fixture here uses -- so RBAC tests
    exercise the actual boundary a real in-cluster deployment would be bound by.
    `BatchV1Api`/`CustomObjectsApi` added alongside `rbac/optimization-role.yaml`
    (Jobs, `metrics.k8s.io`) -- the latter specifically because `kubectl auth
    can-i` was found live to give a wrong answer for this aggregated API, so a
    real authenticated call through this exact client is the only trustworthy
    check for it (see KUBERNETES_CONCEPTS.md).

    `verify_ssl=False` is specific to this local, throwaway kind cluster used
    only to test authorization (not transport security); never appropriate
    against a real cluster.
    """
    admin_host = live_cluster_clients.core.api_client.configuration.host
    sa_config = k8s_client.Configuration()
    sa_config.host = admin_host
    sa_config.verify_ssl = False
    sa_config.api_key["authorization"] = kubesentinel_sa_token
    sa_config.api_key_prefix["authorization"] = "Bearer"
    api_client = k8s_client.ApiClient(sa_config)
    return (
        k8s_client.CoreV1Api(api_client),
        k8s_client.AppsV1Api(api_client),
        k8s_client.BatchV1Api(api_client),
        k8s_client.CustomObjectsApi(api_client),
    )


def kubectl_can_i(
    verb: str, resource: str, *, namespace: str | None = None, subresource: str = "", resource_name: str = ""
) -> bool:
    """Shells out to `kubectl auth can-i --as=...`, the authoritative RBAC
    decision (a real SelfSubjectAccessReview against the API server) -- the
    exact command used to manually confirm the RBAC boundary in Milestone 4
    (see TODO.md #14), now automated and repeatable instead of a one-off.

    `resource_name` checks a `resourceNames`-scoped rule (e.g. the network
    engine's `rbac/network-dns-role.yaml`, which grants `get` on only
    `coredns`/`kube-dns` by name) -- `kubectl`'s own `TYPE/NAME` syntax,
    distinct from `subresource`'s `TYPE/SUBRESOURCE` (callers pass at most
    one of the two).
    """
    resource_arg = resource
    if subresource:
        resource_arg = f"{resource}/{subresource}"
    elif resource_name:
        resource_arg = f"{resource}/{resource_name}"
    cmd = [
        "kubectl",
        "auth",
        "can-i",
        verb,
        resource_arg,
        "--as=system:serviceaccount:kubesentinel-lab:kubesentinel",
    ]
    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return result.stdout.strip() == "yes"


@pytest.fixture(scope="session")
def ensure_remediation_test_deployment(live_cluster_clients: ClusterClients) -> Iterator[None]:
    """Skips unless the `remediation-test` Deployment exists, healthy, at its
    expected steady state (2/2) -- the e2e remediation lifecycle test mutates
    this real Deployment and must restore it afterward, never leaving the lab in
    a different state than it found it."""
    try:
        dep = live_cluster_clients.apps.read_namespaced_deployment(
            name=REMEDIATION_TEST_DEPLOYMENT, namespace=LAB_NAMESPACE
        )
    except ApiException as exc:
        pytest.skip(
            f"'{REMEDIATION_TEST_DEPLOYMENT}' Deployment not present -- apply "
            f"lab/manifests/remediation-test/deployment.yaml: {exc}"
        )
    if dep.spec.replicas != 2:
        pytest.skip(
            f"'{REMEDIATION_TEST_DEPLOYMENT}' is at {dep.spec.replicas} replicas, not its "
            "expected steady state of 2 -- leave it alone until it's reconciled back"
        )
    yield

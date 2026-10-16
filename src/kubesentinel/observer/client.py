"""Kubernetes API client construction.

Supports in-cluster config (when running as a Pod, using the mounted service account)
and local kubeconfig (for development). Which one is used is never guessed from
environment heuristics beyond the standard "are we in a Pod" check that the official
client itself performs.
"""

from __future__ import annotations

import logging

from kubernetes import client, config

from kubesentinel.config import Settings

logger = logging.getLogger(__name__)


class ClusterClients:
    """Bundle of the typed API clients the observer, remediation, and
    optimization layers need.

    `custom_objects` and `batch` were added for the optimization engine
    (`metrics.k8s.io` has no typed client in this project's pinned
    `kubernetes` version and must be queried via `CustomObjectsApi`; Jobs
    need `BatchV1Api`, not previously constructed anywhere) -- purely
    additive, every existing caller of `core`/`apps`/`discovery` is
    unaffected. `networking` was added for the network troubleshooting
    engine (NetworkPolicy has no client on any of the existing five).
    """

    def __init__(
        self,
        core: client.CoreV1Api,
        apps: client.AppsV1Api,
        discovery: client.DiscoveryV1Api,
        custom_objects: client.CustomObjectsApi,
        batch: client.BatchV1Api,
        networking: client.NetworkingV1Api,
    ):
        self.core = core
        self.apps = apps
        self.discovery = discovery
        self.custom_objects = custom_objects
        self.batch = batch
        self.networking = networking


def load_kube_config(settings: Settings) -> None:
    """Load kube config, preferring in-cluster when available."""
    try:
        config.load_incluster_config()
        logger.info("loaded in-cluster kubeconfig")
        return
    except config.ConfigException:
        pass

    config.load_kube_config(
        config_file=settings.kubeconfig or None,
        context=settings.kube_context or None,
    )
    logger.info(
        "loaded local kubeconfig",
        extra={"context": settings.kube_context or "<current-context>"},
    )


def build_clients(settings: Settings) -> ClusterClients:
    load_kube_config(settings)
    api_client = client.ApiClient()
    return ClusterClients(
        core=client.CoreV1Api(api_client),
        apps=client.AppsV1Api(api_client),
        discovery=client.DiscoveryV1Api(api_client),
        custom_objects=client.CustomObjectsApi(api_client),
        batch=client.BatchV1Api(api_client),
        networking=client.NetworkingV1Api(api_client),
    )

"""Runs the real ObserverService + IncidentManager against the real kind
cluster's already-applied `readiness-probe-failure` scenario, through a
temporary SQLite repository -- not a mock, not hand-built evidence. Skips if
that scenario isn't currently applied (run
`./lab/scripts/run-scenario.sh readiness-probe-failure` or `./scripts/demo.sh`
first) rather than silently applying it itself: these tests observe the
cluster, they don't own writing failures into it.

Uses the probe-failure scenario rather than `crashloop` specifically because
its failure signal (a recurring `Unhealthy` Event, `periodSeconds=3`, forever)
is reliably visible within a short poll window regardless of exactly when the
test starts -- see `probe_failure_pod_name`'s docstring in conftest.py for the
crashloop-specific timing trap this was found to have live.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kubesentinel.config import Settings
from kubesentinel.incidents.manager import IncidentManager
from kubesentinel.observer.service import ObserverService
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from tests.conftest import LAB_NAMESPACE
from tests.integration.conftest import poll_until

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")


def test_observer_opens_real_incident_for_live_probe_failure_pod(live_cluster_clients, probe_failure_pod_name):
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = SQLiteIncidentRepository(str(Path(tmpdir) / "observer-live.db"))
        try:
            manager = IncidentManager(repo)
            settings = Settings(watch_namespaces=LAB_NAMESPACE, resync_interval_seconds=300)
            observer = ObserverService(live_cluster_clients, settings, manager)
            observer.start()
            try:
                # Polls for THIS specific pod's incident, not "any incident in
                # the namespace" -- found live to matter: the lab namespace
                # has several other scenarios also broken at once, each
                # handled by a different watcher thread, so a weaker "is
                # *some* incident open yet" predicate can be satisfied by an
                # unrelated scenario before this Event-driven one has been
                # processed, making poll_until return too early regardless of
                # how long its timeout is.
                matching = poll_until(
                    lambda: [
                        i
                        for i in repo.list(status="DIAGNOSED", namespace=LAB_NAMESPACE)
                        if i.resource.name == probe_failure_pod_name
                    ]
                )
            finally:
                observer.stop(timeout=5.0)

            assert matching, f"no DIAGNOSED incident opened for the actual pod {probe_failure_pod_name}"
            assert matching[0].category == "READINESS_PROBE_FAILING"
        finally:
            repo.close()


def test_observer_still_detects_after_pod_deleted_and_recreated_with_same_name(
    live_cluster_clients, pending_resources_pod_name
):
    """Regression-style live check for the UID-based dedup design (TODO.md #1/#6):
    deleting the live Pod and letting the Deployment recreate it under the same
    name must produce a *new* incident (new UID), never silently reuse the old
    incident record for a resource that is, identity-wise, a different object.

    Uses the `pending-resources` scenario specifically -- see
    `pending_resources_pod_name`'s docstring in conftest.py for why
    `readiness-probe-failure-demo` (used by the other tests in this file) isn't
    a reliable choice for *this* particular test.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = SQLiteIncidentRepository(str(Path(tmpdir) / "observer-dedup.db"))
        try:
            manager = IncidentManager(repo)
            settings = Settings(watch_namespaces=LAB_NAMESPACE, resync_interval_seconds=300)
            observer = ObserverService(live_cluster_clients, settings, manager)
            observer.start()
            try:
                first_round = poll_until(
                    lambda: [
                        i
                        for i in repo.list(namespace=LAB_NAMESPACE)
                        if i.resource.name == pending_resources_pod_name
                    ]
                )
                assert first_round, "expected an incident for the original pending-resources pod"
                original_uid = first_round[0].resource.uid

                live_cluster_clients.core.delete_namespaced_pod(
                    name=pending_resources_pod_name, namespace=LAB_NAMESPACE
                )

                def new_pod_incident():
                    pods = live_cluster_clients.core.list_namespaced_pod(
                        namespace=LAB_NAMESPACE, label_selector="app=pending-resources-demo"
                    )
                    if not pods.items:
                        return None
                    new_uid = pods.items[0].metadata.uid
                    if new_uid == original_uid:
                        return None  # still the same object, or not yet replaced
                    matches = [
                        i for i in repo.list(namespace=LAB_NAMESPACE) if i.resource.uid == new_uid
                    ]
                    return matches or None

                new_round = poll_until(new_pod_incident, timeout=30.0)
            finally:
                observer.stop(timeout=5.0)

            assert new_round, "expected a new incident for the recreated pod's new UID"
            assert new_round[0].resource.uid != original_uid
        finally:
            repo.close()

"""Live check that a simulated KubeSentinel restart -- a fresh process pointed
at the same SQLite file -- correctly reuses an already-open incident's identity
(never duplicates it) and that the Milestone 7 `reconcile_metrics()` fix really
makes `incidents_active` reflect persisted state immediately, before the new
process's own observer has produced a single fresh detection.

This is the live counterpart to TODO.md #18 (found and fixed against this exact
scenario, originally observed on a long-running demo server) and to the unit
tests in test_incident_manager.py (which exercise `reconcile_metrics()` against
a repository directly, not through a real cluster + real ObserverService).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kubesentinel.config import Settings
from kubesentinel.incidents.manager import IncidentManager
from kubesentinel.metrics.registry import incidents_active
from kubesentinel.observer.service import ObserverService
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from tests.conftest import LAB_NAMESPACE
from tests.integration.conftest import poll_until

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")


def test_restart_reuses_incident_identity_and_reconciles_metrics(live_cluster_clients, probe_failure_pod_name):
    settings = Settings(watch_namespaces=LAB_NAMESPACE, resync_interval_seconds=300)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "restart-recovery.db")

        # "Process 1": first detection against a brand-new db.
        repo1 = SQLiteIncidentRepository(db_path)
        try:
            manager1 = IncidentManager(repo1)
            observer1 = ObserverService(live_cluster_clients, settings, manager1)
            observer1.start()
            try:
                first = poll_until(
                    lambda: [
                        i
                        for i in repo1.list(namespace=LAB_NAMESPACE)
                        if i.resource.name == probe_failure_pod_name and i.resource.kind == "Pod"
                    ]
                )
            finally:
                observer1.stop(timeout=5.0)
        finally:
            repo1.close()

        assert first, "expected process 1 to open an incident for the live probe-failure pod"
        original_incident_id = first[0].incident_id

        # Simulate what a fresh process's module-level Prometheus Gauge actually
        # looks like before anything touches it: force it to a state a real
        # restart would start from (0), to prove reconcile_metrics -- not test
        # pollution from some other fixture -- is what fixes it below.
        incidents_active.labels(category="READINESS_PROBE_FAILING").set(0)

        # "Process 2": simulate a KubeSentinel restart against the SAME db file.
        repo2 = SQLiteIncidentRepository(db_path)
        try:
            manager2 = IncidentManager(repo2)
            manager2.reconcile_metrics()

            gauge_value = incidents_active.labels(category="READINESS_PROBE_FAILING")._value.get()
            assert gauge_value >= 1, (
                "incidents_active must already reflect the persisted open incident "
                "immediately after reconcile_metrics(), before any new observation"
            )

            observer2 = ObserverService(live_cluster_clients, settings, manager2)
            observer2.start()
            try:
                second = poll_until(
                    lambda: [
                        i
                        for i in repo2.list(namespace=LAB_NAMESPACE)
                        if i.resource.name == probe_failure_pod_name and i.resource.kind == "Pod"
                    ]
                )
            finally:
                observer2.stop(timeout=5.0)
        finally:
            repo2.close()

        assert second, "expected process 2 to still see an incident for this pod"
        assert second[0].incident_id == original_incident_id, (
            "a resource already tracked before the restart must dedupe into the SAME "
            "incident -- never open a duplicate just because a fresh process observed "
            "it again"
        )

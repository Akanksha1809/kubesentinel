"""Multi-threaded exercise of `RemediationService.propose()`: the per-incident
action budget is read-then-decide-then-persist across a repository read and a
later write, so its correctness under genuine concurrency can't be shown by a
single-threaded unit test -- the race only exists when callers actually
overlap in time. TESTING.md (written at Milestone 1) flagged exactly this
("concurrent remediation requests") as something Milestone 9 needed to
actually exercise, not just assert about.

Uses a real `SQLiteIncidentRepository` (a real file, not mocked) and
`APPROVAL_REQUIRED` policy mode, which never calls the executor/verifier --
`propose()` only evaluates policy and persists in that mode, so no live
cluster mutation happens here; the live cluster clients are only plumbed
through to `RemediationExecutor`/`RecoveryVerifier` because the service
requires them, not because this test calls either.
"""

from __future__ import annotations

import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from kubesentinel.config import PolicyMode, Settings
from kubesentinel.models.action import ActionStatus, ActionType
from kubesentinel.models.incident import Incident, IncidentSeverity, IncidentStatus, ResourceRef
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from kubesentinel.policy.engine import PolicyEngine
from kubesentinel.remediation.executor import RemediationExecutor
from kubesentinel.remediation.service import RemediationService
from kubesentinel.verification.verifier import RecoveryVerifier
from tests.conftest import LAB_NAMESPACE, REMEDIATION_TEST_DEPLOYMENT

pytestmark = pytest.mark.usefixtures("lab_namespace_ready")

_CONCURRENCY = 10
_BUDGET = 3


def _build_service(db_path: str, live_cluster_clients) -> tuple[RemediationService, SQLiteIncidentRepository]:
    settings = Settings(
        policy_mode=PolicyMode.APPROVAL_REQUIRED,
        action_allowed_namespaces=LAB_NAMESPACE,
        max_actions_per_incident=_BUDGET,
        max_actions_per_window=1000,
        action_cooldown_seconds=0,
    )
    repo = SQLiteIncidentRepository(db_path)
    policy = PolicyEngine(settings)
    executor = RemediationExecutor(live_cluster_clients.apps)
    verifier = RecoveryVerifier(live_cluster_clients.apps)
    service = RemediationService(policy, executor, verifier, repo, settings)
    return service, repo


def _make_incident() -> Incident:
    now = datetime.now(timezone.utc)
    return Incident(
        incident_id=str(uuid4()),
        resource=ResourceRef(
            namespace=LAB_NAMESPACE,
            kind="Deployment",
            name=REMEDIATION_TEST_DEPLOYMENT,
            # Never read by propose() itself (only the executor would, and
            # APPROVAL_REQUIRED mode never reaches the executor from here) --
            # a fabricated UID is fine.
            uid=str(uuid4()),
        ),
        category="ROLLOUT_FAILED",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.DIAGNOSED,
        first_observed_at=now,
        last_observed_at=now,
    )


def test_concurrent_proposals_never_exceed_the_per_incident_budget(live_cluster_clients):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "concurrent.db")
        service, repo = _build_service(db_path, live_cluster_clients)
        try:
            incident = _make_incident()
            repo.save(incident)

            barrier = threading.Barrier(_CONCURRENCY)
            results: list[ActionStatus] = []
            errors: list[BaseException] = []
            results_lock = threading.Lock()

            def worker() -> None:
                try:
                    barrier.wait(timeout=5)
                    action = service.propose(incident, ActionType.RESTART_DEPLOYMENT, rationale="concurrency test")
                    with results_lock:
                        results.append(action.status)
                except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`, not swallowed
                    with results_lock:
                        errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(_CONCURRENCY)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors, f"propose() raised under concurrency: {errors}"
            assert len(results) == _CONCURRENCY

            accepted = [s for s in results if s != ActionStatus.REJECTED]
            rejected = [s for s in results if s == ActionStatus.REJECTED]
            assert len(accepted) == _BUDGET, (
                f"expected exactly {_BUDGET} proposals accepted under the per-incident budget "
                f"despite {_CONCURRENCY} concurrent callers racing the same incident, got "
                f"{len(accepted)}: {results}"
            )
            assert len(rejected) == _CONCURRENCY - _BUDGET

            persisted = repo.get(incident.incident_id)
            assert persisted is not None
            assert len(persisted.actions) == _CONCURRENCY, "every proposal must still be recorded, accepted or not"
            assert sum(1 for a in persisted.actions if a.status != ActionStatus.REJECTED) == _BUDGET
        finally:
            repo.close()

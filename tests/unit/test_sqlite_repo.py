import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from kubesentinel.models.action import ActionStatus, ActionTarget, ActionType, RemediationAction
from kubesentinel.models.incident import Incident, IncidentStatus, ResourceRef
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = SQLiteIncidentRepository(str(Path(d) / "test.db"))
        yield r
        r.close()


def _incident(uid: str, status: IncidentStatus = IncidentStatus.OPEN) -> Incident:
    now = datetime.now(timezone.utc)
    return Incident(
        incident_id=str(uuid4()),
        resource=ResourceRef(namespace="kubesentinel-lab", kind="Pod", name="demo", uid=uid),
        status=status,
        first_observed_at=now,
        last_observed_at=now,
    )


def test_save_and_get_roundtrip(repo):
    incident = _incident("uid-1")
    repo.save(incident)

    loaded = repo.get(incident.incident_id)
    assert loaded is not None
    assert loaded.incident_id == incident.incident_id
    assert loaded.resource.uid == "uid-1"


def test_get_missing_returns_none(repo):
    assert repo.get("does-not-exist") is None


def test_find_open_by_resource_uid_excludes_resolved(repo):
    incident = _incident("uid-2", status=IncidentStatus.RESOLVED)
    repo.save(incident)
    assert repo.find_open_by_resource_uid("uid-2") is None


def test_find_open_by_resource_uid_finds_open(repo):
    incident = _incident("uid-3", status=IncidentStatus.OPEN)
    repo.save(incident)
    found = repo.find_open_by_resource_uid("uid-3")
    assert found is not None
    assert found.incident_id == incident.incident_id


def test_save_is_an_upsert(repo):
    incident = _incident("uid-4")
    repo.save(incident)

    incident.status = IncidentStatus.RESOLVED
    repo.save(incident)

    loaded = repo.get(incident.incident_id)
    assert loaded.status == IncidentStatus.RESOLVED
    assert len(repo.list()) == 1


def test_list_filters_by_namespace_and_status(repo):
    repo.save(_incident("uid-5", status=IncidentStatus.OPEN))
    repo.save(_incident("uid-6", status=IncidentStatus.RESOLVED))

    assert len(repo.list(status="OPEN")) == 1
    assert len(repo.list(namespace="kubesentinel-lab")) == 2
    assert len(repo.list(namespace="other-namespace")) == 0


def _action(incident_id: str, proposed_at: datetime) -> RemediationAction:
    return RemediationAction(
        action_id=str(uuid4()),
        incident_id=incident_id,
        action_type=ActionType.RESTART_DEPLOYMENT,
        target=ActionTarget(namespace="kubesentinel-lab", resource_type="Deployment", resource_name="demo"),
        rationale="test",
        proposed_at=proposed_at,
    )


def test_list_actions_since_spans_incidents_and_filters_by_time(repo):
    now = datetime.now(timezone.utc)
    recent_incident = _incident("uid-7")
    recent_incident.actions = [_action(recent_incident.incident_id, now - timedelta(seconds=10))]
    repo.save(recent_incident)

    old_incident = _incident("uid-8")
    old_incident.actions = [_action(old_incident.incident_id, now - timedelta(seconds=9999))]
    repo.save(old_incident)

    results = repo.list_actions_since(now - timedelta(seconds=60))
    assert len(results) == 1
    assert results[0].incident_id == recent_incident.incident_id


def test_list_actions_since_returns_empty_when_none_match(repo):
    repo.save(_incident("uid-9"))
    assert repo.list_actions_since(datetime.now(timezone.utc)) == []


def _standalone_action(proposed_at: datetime) -> RemediationAction:
    return RemediationAction(
        action_id=str(uuid4()),
        incident_id=None,
        action_type=ActionType.ADJUST_RESOURCES,
        target=ActionTarget(namespace="kubesentinel-lab", resource_type="Deployment", resource_name="demo"),
        rationale="test",
        proposed_at=proposed_at,
    )


class TestStandaloneActions:
    def test_save_and_get_roundtrip(self, repo):
        action = _standalone_action(datetime.now(timezone.utc))
        repo.save_standalone_action(action)

        loaded = repo.get_standalone_action(action.action_id)
        assert loaded is not None
        assert loaded.action_id == action.action_id
        assert loaded.incident_id is None

    def test_get_missing_returns_none(self, repo):
        assert repo.get_standalone_action("does-not-exist") is None

    def test_save_is_an_upsert(self, repo):
        action = _standalone_action(datetime.now(timezone.utc))
        repo.save_standalone_action(action)

        action.status = ActionStatus.EXECUTED
        repo.save_standalone_action(action)

        loaded = repo.get_standalone_action(action.action_id)
        assert loaded.status == ActionStatus.EXECUTED

    def test_list_actions_since_includes_standalone_actions_alongside_incident_ones(self, repo):
        now = datetime.now(timezone.utc)
        incident = _incident("uid-10")
        incident.actions = [_action(incident.incident_id, now - timedelta(seconds=10))]
        repo.save(incident)

        standalone = _standalone_action(now - timedelta(seconds=5))
        repo.save_standalone_action(standalone)

        results = repo.list_actions_since(now - timedelta(seconds=60))
        assert len(results) == 2
        assert any(a.incident_id is None for a in results)
        assert any(a.incident_id == incident.incident_id for a in results)

    def test_list_actions_since_filters_standalone_actions_by_time(self, repo):
        now = datetime.now(timezone.utc)
        repo.save_standalone_action(_standalone_action(now - timedelta(seconds=9999)))

        assert repo.list_actions_since(now - timedelta(seconds=60)) == []

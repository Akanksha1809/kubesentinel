from datetime import datetime, timezone
from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from kubesentinel.models.action import ActionStatus, ActionTarget, ActionType, RemediationAction
from kubesentinel.remediation.executor import RemediationExecutor
from tests.unit.factories import make_deployment, make_replica_set


def _target(resource_uid: str = "deploy-uid-1234") -> ActionTarget:
    return ActionTarget(
        namespace="kubesentinel-lab",
        resource_type="Deployment",
        resource_name="test-deployment",
        resource_uid=resource_uid,
    )


def _action(action_type: ActionType, target: ActionTarget | None = None, parameters: dict | None = None) -> RemediationAction:
    return RemediationAction(
        action_id="action-1",
        incident_id="incident-1",
        action_type=action_type,
        target=target or _target(),
        rationale="test",
        parameters=parameters or {},
        proposed_at=datetime.now(timezone.utc),
    )


def test_restart_succeeds_and_patches_restart_annotation():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    apps.patch_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    executor = RemediationExecutor(apps)

    action = _action(ActionType.RESTART_DEPLOYMENT)
    result = executor.execute(action)

    assert result.status == ActionStatus.EXECUTED
    assert result.execution_error is None
    assert "restarted_at" in result.execution_result
    patch_body = apps.patch_namespaced_deployment.call_args.kwargs["body"]
    assert "kubectl.kubernetes.io/restartedAt" in patch_body["spec"]["template"]["metadata"]["annotations"]


def test_restart_aborts_when_uid_changed():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(uid="a-different-uid")
    executor = RemediationExecutor(apps)

    action = _action(ActionType.RESTART_DEPLOYMENT, target=_target(resource_uid="deploy-uid-1234"))
    result = executor.execute(action)

    assert result.status == ActionStatus.EXECUTION_FAILED
    assert "changed" in result.execution_error
    apps.patch_namespaced_deployment.assert_not_called()


def test_scale_succeeds_and_records_previous_and_new_replicas():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234", desired_replicas=1)
    executor = RemediationExecutor(apps)

    action = _action(ActionType.SCALE_DEPLOYMENT, parameters={"replicas": 3})
    result = executor.execute(action)

    assert result.status == ActionStatus.EXECUTED
    assert result.execution_result == {"previous_replicas": 1, "new_replicas": 3}
    apps.patch_namespaced_deployment_scale.assert_called_once()
    assert apps.patch_namespaced_deployment_scale.call_args.kwargs["body"] == {"spec": {"replicas": 3}}


def test_rollback_picks_second_most_recent_revision_by_default():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    rs_v1 = make_replica_set(revision="1", owner_uid="deploy-uid-1234", image="busybox:v1")
    rs_v2 = make_replica_set(revision="2", owner_uid="deploy-uid-1234", image="busybox:v2")
    apps.list_namespaced_replica_set.return_value = MagicMock(items=[rs_v1, rs_v2])
    executor = RemediationExecutor(apps)

    action = _action(ActionType.ROLLBACK_DEPLOYMENT)
    result = executor.execute(action)

    assert result.status == ActionStatus.EXECUTED
    assert result.execution_result == {"from_revision": 2, "to_revision": 1}
    # A full replace, not a merge patch -- see the executor's own comment for why
    # a merge patch cannot correctly *remove* a field the newer revision added.
    replaced_body = apps.replace_namespaced_deployment.call_args.kwargs["body"]
    assert replaced_body.spec.template is rs_v1.spec.template
    apps.patch_namespaced_deployment.assert_not_called()


def test_rollback_to_specific_requested_revision():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    rs_v1 = make_replica_set(revision="1", owner_uid="deploy-uid-1234")
    rs_v2 = make_replica_set(revision="2", owner_uid="deploy-uid-1234")
    rs_v3 = make_replica_set(revision="3", owner_uid="deploy-uid-1234")
    apps.list_namespaced_replica_set.return_value = MagicMock(items=[rs_v1, rs_v2, rs_v3])
    executor = RemediationExecutor(apps)

    action = _action(ActionType.ROLLBACK_DEPLOYMENT, parameters={"revision": 1})
    result = executor.execute(action)

    assert result.execution_result == {"from_revision": 3, "to_revision": 1}


def test_rollback_fails_with_fewer_than_two_revisions():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    apps.list_namespaced_replica_set.return_value = MagicMock(
        items=[make_replica_set(revision="1", owner_uid="deploy-uid-1234")]
    )
    executor = RemediationExecutor(apps)

    action = _action(ActionType.ROLLBACK_DEPLOYMENT)
    result = executor.execute(action)

    assert result.status == ActionStatus.EXECUTION_FAILED
    assert "no prior revision" in result.execution_error
    apps.patch_namespaced_deployment.assert_not_called()
    apps.replace_namespaced_deployment.assert_not_called()


def test_rollback_replaces_the_whole_deployment_object_not_a_patch_body():
    # Regression test for a bug found live: rolling back to a template that
    # *lacks* an annotation the current template has (e.g. a restart annotation)
    # silently did nothing under a merge PATCH, because the target's absent
    # field serializes to nothing in the patch body, and merge-patch semantics
    # treat "absent" as "no change," not "clear it." Only a full replace actually
    # removes a field the newer revision added.
    apps = MagicMock()
    deployment = make_deployment(uid="deploy-uid-1234")
    apps.read_namespaced_deployment.return_value = deployment
    rs_v1 = make_replica_set(revision="1", owner_uid="deploy-uid-1234")  # no restart annotation
    rs_v2 = make_replica_set(revision="2", owner_uid="deploy-uid-1234")
    apps.list_namespaced_replica_set.return_value = MagicMock(items=[rs_v1, rs_v2])
    executor = RemediationExecutor(apps)

    executor.execute(_action(ActionType.ROLLBACK_DEPLOYMENT))

    # The full Deployment object was passed, with its own spec.template field
    # mutated to the old revision's template -- a replace, not a sparse patch dict.
    replaced_body = apps.replace_namespaced_deployment.call_args.kwargs["body"]
    assert replaced_body is deployment
    assert deployment.spec.template is rs_v1.spec.template


def test_rollback_ignores_replicasets_not_owned_by_this_deployment():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    owned = make_replica_set(revision="1", owner_uid="deploy-uid-1234")
    unrelated = make_replica_set(revision="5", owner_uid="some-other-deployment-uid")
    apps.list_namespaced_replica_set.return_value = MagicMock(items=[owned, unrelated])
    executor = RemediationExecutor(apps)

    result = executor.execute(_action(ActionType.ROLLBACK_DEPLOYMENT))

    # Only one owned ReplicaSet -- not enough to roll back, the unrelated one must
    # not be counted even though it has a higher revision number.
    assert result.status == ActionStatus.EXECUTION_FAILED
    assert "no prior revision" in result.execution_error


def test_api_exception_is_caught_and_recorded():
    apps = MagicMock()
    apps.read_namespaced_deployment.side_effect = ApiException(status=403, reason="Forbidden")
    executor = RemediationExecutor(apps)

    result = executor.execute(_action(ActionType.RESTART_DEPLOYMENT))

    assert result.status == ActionStatus.EXECUTION_FAILED
    assert "403" in result.execution_error


def test_unexpected_exception_is_caught_and_recorded_not_raised():
    apps = MagicMock()
    apps.read_namespaced_deployment.side_effect = RuntimeError("boom")
    executor = RemediationExecutor(apps)

    result = executor.execute(_action(ActionType.RESTART_DEPLOYMENT))

    assert result.status == ActionStatus.EXECUTION_FAILED
    assert "boom" in result.execution_error


def test_recommend_resource_adjustment_never_calls_the_api():
    apps = MagicMock()
    executor = RemediationExecutor(apps)

    action = _action(
        ActionType.RECOMMEND_RESOURCE_ADJUSTMENT, parameters={"recommendation": "raise memory limit to 64Mi"}
    )
    result = executor.execute(action)

    assert result.status == ActionStatus.EXECUTED
    assert result.execution_result == {"recommendation": "raise memory limit to 64Mi"}
    apps.assert_not_called()
    apps.read_namespaced_deployment.assert_not_called()
    apps.patch_namespaced_deployment.assert_not_called()


def test_execute_always_sets_executed_at_and_completed_at():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    apps.patch_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    executor = RemediationExecutor(apps)

    result = executor.execute(_action(ActionType.RESTART_DEPLOYMENT))

    assert result.executed_at is not None
    assert result.completed_at is not None


def test_request_timeout_is_passed_to_the_api_call():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    apps.patch_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
    executor = RemediationExecutor(apps, timeout_seconds=7)

    executor.execute(_action(ActionType.RESTART_DEPLOYMENT))

    assert apps.read_namespaced_deployment.call_args.kwargs["_request_timeout"] == 7
    assert apps.patch_namespaced_deployment.call_args.kwargs["_request_timeout"] == 7


class TestAdjustResources:
    def test_patches_only_the_proposed_resource_keys(self):
        apps = MagicMock()
        apps.read_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
        apps.patch_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
        executor = RemediationExecutor(apps)

        action = _action(
            ActionType.ADJUST_RESOURCES,
            parameters={"container_name": "app", "requests": {"cpu": "500m"}, "limits": {}},
        )
        result = executor.execute(action)

        assert result.status == ActionStatus.EXECUTED
        patch_body = apps.patch_namespaced_deployment.call_args.kwargs["body"]
        container_patch = patch_body["spec"]["template"]["spec"]["containers"][0]
        assert container_patch["name"] == "app"
        assert container_patch["resources"] == {"requests": {"cpu": "500m"}}

    def test_patches_both_requests_and_limits_when_both_proposed(self):
        apps = MagicMock()
        apps.read_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
        apps.patch_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
        executor = RemediationExecutor(apps)

        action = _action(
            ActionType.ADJUST_RESOURCES,
            parameters={
                "container_name": "app",
                "requests": {"cpu": "500m", "memory": "256Mi"},
                "limits": {"memory": "512Mi"},
            },
        )
        executor.execute(action)

        resources = apps.patch_namespaced_deployment.call_args.kwargs["body"]["spec"]["template"]["spec"]["containers"][0]["resources"]
        assert resources["requests"] == {"cpu": "500m", "memory": "256Mi"}
        assert resources["limits"] == {"memory": "512Mi"}

    def test_no_typed_object_anywhere_in_the_patch_body(self):
        """The `.to_dict()` footgun TODO.md #11 caught for a different
        patch body cannot recur here if every value in this body is a
        plain dict/str/list -- confirmed directly rather than assumed."""
        apps = MagicMock()
        apps.read_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
        apps.patch_namespaced_deployment.return_value = make_deployment(uid="deploy-uid-1234")
        executor = RemediationExecutor(apps)

        action = _action(
            ActionType.ADJUST_RESOURCES,
            parameters={"container_name": "app", "requests": {"cpu": "500m"}, "limits": {}},
        )
        executor.execute(action)

        def _assert_plain(value):
            assert isinstance(value, (dict, str, list)), f"unexpected typed object in patch body: {type(value)}"
            if isinstance(value, dict):
                for v in value.values():
                    _assert_plain(v)
            elif isinstance(value, list):
                for v in value:
                    _assert_plain(v)

        _assert_plain(apps.patch_namespaced_deployment.call_args.kwargs["body"])

    def test_aborts_when_uid_changed(self):
        apps = MagicMock()
        apps.read_namespaced_deployment.return_value = make_deployment(uid="a-different-uid")
        executor = RemediationExecutor(apps)

        action = _action(
            ActionType.ADJUST_RESOURCES,
            target=_target(resource_uid="deploy-uid-1234"),
            parameters={"container_name": "app", "requests": {"cpu": "500m"}, "limits": {}},
        )
        result = executor.execute(action)

        assert result.status == ActionStatus.EXECUTION_FAILED
        apps.patch_namespaced_deployment.assert_not_called()

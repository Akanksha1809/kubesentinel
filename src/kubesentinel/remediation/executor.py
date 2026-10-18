"""The remediation executor: the only code in this project permitted to mutate
cluster state, and only through the closed `ActionType` enum's specific operations.

Every mutating method here (`restart`, `rollback`, `scale`) follows the same shape,
matching the spec's "before executing an action" checklist:

1. Read the live Deployment and verify its UID still matches what the policy
   decision was made against -- the resource may have changed (or been deleted)
   between diagnosis and execution, which the spec explicitly calls out as a test
   case ("the workload changes before the action executes").
2. Perform the one, narrowly-scoped API call for this action type, with a hard
   timeout so a hung API server fails fast rather than blocking forever.
3. Never raise to the caller: every failure mode is caught and recorded as a
   structured result on the action itself (`execution_result` / `execution_error`),
   because the point of this module is to make outcomes auditable, not to let an
   exception silently vanish a failed remediation attempt.

`RECOMMEND_*` action types never call the Kubernetes API at all -- they only ever
produce advisory text from the evidence/hypothesis already on hand.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from kubernetes import client
from kubernetes.client.rest import ApiException

from kubesentinel.models.action import ActionStatus, ActionTarget, ActionType, RemediationAction

logger = logging.getLogger(__name__)


class ResourceChangedError(Exception):
    """Raised internally when the live resource's UID no longer matches the
    target the action was proposed against. Always caught -- never escapes
    `RemediationExecutor.execute`."""


class RemediationExecutor:
    def __init__(self, apps_api: client.AppsV1Api, timeout_seconds: int = 30):
        self._apps = apps_api
        self._timeout = timeout_seconds

    def execute(self, action: RemediationAction) -> RemediationAction:
        """Mutates and returns `action` with status/result/error/timestamps set.

        Never raises -- every failure path is caught and recorded on the action.
        """
        action.executed_at = datetime.now(timezone.utc)
        try:
            if action.action_type == ActionType.RESTART_DEPLOYMENT:
                result = self._restart(action.target)
            elif action.action_type == ActionType.ROLLBACK_DEPLOYMENT:
                result = self._rollback(action.target, action.parameters.get("revision"))
            elif action.action_type == ActionType.SCALE_DEPLOYMENT:
                result = self._scale(action.target, action.parameters["replicas"])
            elif action.action_type == ActionType.ADJUST_RESOURCES:
                result = self._adjust_resources(action.target, action.parameters)
            elif action.action_type == ActionType.RECOMMEND_RESOURCE_ADJUSTMENT:
                result = self._recommend_resource_adjustment(action.parameters)
            elif action.action_type == ActionType.RECOMMEND_CONFIG_FIX:
                result = self._recommend_config_fix(action.parameters)
            else:  # pragma: no cover - exhaustive over the closed ActionType enum
                raise ValueError(f"unhandled action type: {action.action_type}")

            action.execution_result = result
            action.status = ActionStatus.EXECUTED
        except ResourceChangedError as exc:
            action.execution_error = str(exc)
            action.status = ActionStatus.EXECUTION_FAILED
            logger.warning("remediation aborted: resource changed", extra={"action_id": action.action_id})
        except ApiException as exc:
            action.execution_error = f"Kubernetes API error (status={exc.status}): {exc.reason}"
            action.status = ActionStatus.EXECUTION_FAILED
            logger.warning(
                "remediation failed: API error",
                extra={"action_id": action.action_id, "status": exc.status},
            )
        except Exception as exc:  # noqa: BLE001 - every failure must be recorded, never raised
            action.execution_error = f"{type(exc).__name__}: {exc}"
            action.status = ActionStatus.EXECUTION_FAILED
            logger.exception("remediation failed unexpectedly", extra={"action_id": action.action_id})
        finally:
            action.completed_at = datetime.now(timezone.utc)

        return action

    def _verify_identity(self, target: ActionTarget) -> client.V1Deployment:
        deployment = self._apps.read_namespaced_deployment(
            name=target.resource_name,
            namespace=target.namespace,
            _request_timeout=self._timeout,
        )
        if target.resource_uid and deployment.metadata.uid != target.resource_uid:
            raise ResourceChangedError(
                f"Deployment '{target.namespace}/{target.resource_name}' has UID "
                f"{deployment.metadata.uid}, expected {target.resource_uid} -- it changed "
                "(deleted and recreated) since this action was proposed; refusing to act on it."
            )
        return deployment

    def _restart(self, target: ActionTarget) -> dict:
        deployment = self._verify_identity(target)
        restarted_at = datetime.now(timezone.utc).isoformat()
        # Exactly what `kubectl rollout restart` does under the hood: touch an
        # annotation on the pod template, which is enough to trigger a new
        # rollout without changing anything else about the Deployment.
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": restarted_at}}
                }
            }
        }
        updated = self._apps.patch_namespaced_deployment(
            name=target.resource_name,
            namespace=target.namespace,
            body=patch,
            _request_timeout=self._timeout,
        )
        return {
            "restarted_at": restarted_at,
            "previous_resource_version": deployment.metadata.resource_version,
            "new_resource_version": updated.metadata.resource_version,
        }

    def _scale(self, target: ActionTarget, replicas: int) -> dict:
        deployment = self._verify_identity(target)
        previous_replicas = deployment.spec.replicas
        self._apps.patch_namespaced_deployment_scale(
            name=target.resource_name,
            namespace=target.namespace,
            body={"spec": {"replicas": replicas}},
            _request_timeout=self._timeout,
        )
        return {"previous_replicas": previous_replicas, "new_replicas": replicas}

    def _adjust_resources(self, target: ActionTarget, parameters: dict) -> dict:
        """Applies a ResourceRecommendation's own proposed requests/limits
        to one container (docs/RESOURCE_OPTIMIZATION_DESIGN.md Section
        14.3). A merge PATCH, like `_restart`, never a REPLACE like
        `_rollback` -- this never needs to *clear* an existing field, only
        set the specific ones the recommendation proposed a number for.

        Every value in this patch body is a plain `dict`/`str` -- never a
        typed client object nested inside a dict, so the `.to_dict()`
        footgun `_rollback`'s own comment warns about (TODO.md #11)
        cannot recur here: there's no typed object anywhere in this body
        to misserialize. Kubernetes' own strategic-merge-patch semantics
        do the rest correctly: `containers` merges by its `name` patch-merge
        key (only the named container is touched), and `resources.requests`/
        `resources.limits` are plain maps, deep-merged key-by-key (setting
        `cpu` leaves an existing `memory` key, if any, untouched).
        """
        deployment = self._verify_identity(target)
        container_name = parameters["container_name"]
        resources: dict = {}
        if parameters.get("requests"):
            resources["requests"] = parameters["requests"]
        if parameters.get("limits"):
            resources["limits"] = parameters["limits"]
        patch = {
            "spec": {
                "template": {
                    "spec": {"containers": [{"name": container_name, "resources": resources}]}
                }
            }
        }
        updated = self._apps.patch_namespaced_deployment(
            name=target.resource_name,
            namespace=target.namespace,
            body=patch,
            _request_timeout=self._timeout,
        )
        return {
            "container_name": container_name,
            "applied": resources,
            "previous_resource_version": deployment.metadata.resource_version,
            "new_resource_version": updated.metadata.resource_version,
        }

    def _rollback(self, target: ActionTarget, requested_revision: int | None) -> dict:
        deployment = self._verify_identity(target)
        selector = deployment.spec.selector.match_labels or {}
        label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
        replica_sets = self._apps.list_namespaced_replica_set(
            namespace=target.namespace,
            label_selector=label_selector,
            _request_timeout=self._timeout,
        ).items

        revisioned = [
            (int(rs.metadata.annotations["deployment.kubernetes.io/revision"]), rs)
            for rs in replica_sets
            if rs.metadata.annotations and "deployment.kubernetes.io/revision" in rs.metadata.annotations
            and any(ref.uid == deployment.metadata.uid for ref in (rs.metadata.owner_references or []))
        ]
        if len(revisioned) < 2:
            raise ValueError(
                f"no prior revision available to roll back to -- found {len(revisioned)} "
                "revision(s) of ReplicaSets owned by this Deployment"
            )
        revisioned.sort(key=lambda pair: pair[0])
        current_revision, _ = revisioned[-1]

        if requested_revision is not None:
            match = next((rs for rev, rs in revisioned if rev == requested_revision), None)
            if match is None:
                raise ValueError(f"revision {requested_revision} not found in this Deployment's history")
            target_revision, target_rs = requested_revision, match
        else:
            target_revision, target_rs = revisioned[-2]

        # A merge PATCH cannot do this correctly: confirmed live, rolling back to
        # a template that *lacks* a field the current one has (e.g. the restart
        # annotation) silently left that field untouched, because the target
        # template's absent field serializes to nothing in the patch body, and
        # "absent" means "no change" under merge-patch semantics -- not "clear
        # it". A full REPLACE of the Deployment with its `spec.template` field set
        # to the old revision's template is the only way to actually remove
        # fields the newer revision added, not just overwrite fields it changed.
        deployment.spec.template = target_rs.spec.template
        self._apps.replace_namespaced_deployment(
            name=target.resource_name,
            namespace=target.namespace,
            body=deployment,
            _request_timeout=self._timeout,
        )
        return {"from_revision": current_revision, "to_revision": target_revision}

    def _recommend_resource_adjustment(self, parameters: dict) -> dict:
        return {"recommendation": parameters.get("recommendation", "no recommendation text provided")}

    def _recommend_config_fix(self, parameters: dict) -> dict:
        return {"recommendation": parameters.get("recommendation", "no recommendation text provided")}

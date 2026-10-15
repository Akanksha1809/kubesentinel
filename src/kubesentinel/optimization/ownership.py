"""Resolves a Pod's full owning-workload chain.

Deliberately separate from `observer/inspector.py::resource_ref_from_pod`,
which only resolves **one** level of ownership (Pod -> its immediate owner,
e.g. a ReplicaSet) -- correct for incident identity (an incident is about a
specific object), wrong for optimization, which needs the Deployment a Pod's
ReplicaSet belongs to, not the ReplicaSet itself: several ReplicaSets
(current + old, mid-rollout) can back one Deployment, and a recommendation
must target the Deployment's own template, never an intermediate ReplicaSet
or an individual Pod. `inspector.py` is not modified -- its one-level
behavior is still exactly right for what it's used for.

Pure functions over already-fetched typed objects, same style as
`inspector.py`: no network calls, no side effects, unit-testable with
hand-built `kubernetes.client` model objects.
"""

from __future__ import annotations

from dataclasses import dataclass

from kubernetes.client import V1Pod, V1ReplicaSet

from kubesentinel.models.optimization import WorkloadKind

# Controller kinds this project resolves to a WorkloadKind directly (no
# further chain-walking needed -- unlike a ReplicaSet, these own Pods
# directly). Extending to a new kind is a deliberate, reviewable addition
# here, matching the spec's "extend other workload types when the design
# supports them," not a silent fallback.
_DIRECT_OWNER_KINDS: dict[str, WorkloadKind] = {
    "StatefulSet": WorkloadKind.STATEFULSET,
    "DaemonSet": WorkloadKind.DAEMONSET,
    "Job": WorkloadKind.JOB,
}


@dataclass(frozen=True)
class ResolvedOwner:
    kind: WorkloadKind
    name: str
    uid: str


def _controller_ref(owner_references: list | None):
    if not owner_references:
        return None
    # A Kubernetes object can carry several ownerReferences (e.g. a
    # finalizer-holding controller alongside the real one); at most one has
    # controller=True, and that is the one that actually manages this
    # object's lifecycle. Falling back to the first entry when none is
    # marked is a defensive default for malformed/unusual objects, not the
    # expected path for anything this project's own lab produces.
    for ref in owner_references:
        if ref.controller:
            return ref
    return owner_references[0]


def resolve_pod_owner(pod: V1Pod, replicasets_by_uid: dict[str, V1ReplicaSet]) -> ResolvedOwner | None:
    """Resolves the full owner chain for `pod`.

    - No owner reference at all -> a standalone Pod: `WorkloadKind.POD`.
    - Immediate owner is a StatefulSet/DaemonSet/Job -> resolved directly
      (these own Pods without an intermediate controller).
    - Immediate owner is a ReplicaSet -> walks to *that ReplicaSet's own*
      controller (expected to be a Deployment). `replicasets_by_uid` must
      already contain every ReplicaSet in the Pod's namespace (the caller
      fetches this once per inventory pass, not per Pod, to avoid an N+1
      API call pattern).
    - Immediate owner is a ReplicaSet not present in `replicasets_by_uid`
      (the Pod was listed but its ReplicaSet wasn't -- a genuine race during
      collection, or cluster drift between the two list calls) -> returns
      `None`. The caller must not guess; it's expected to log this and
      either retry or exclude the Pod from this analysis pass rather than
      silently misattribute it.
    - Immediate owner is a kind this project doesn't recognize (a CRD-based
      operator, Argo Rollout, etc.) -> returns `None` for the same reason:
      silently treating an unrecognized controller's Pod as "standalone"
      would misrepresent a perfectly well-managed workload as an orphan,
      which is worse than visibly declining to classify it.
    """
    meta = pod.metadata
    ref = _controller_ref(meta.owner_references if meta else None)
    if ref is None:
        return ResolvedOwner(kind=WorkloadKind.POD, name=meta.name, uid=meta.uid)

    if ref.kind in _DIRECT_OWNER_KINDS:
        return ResolvedOwner(kind=_DIRECT_OWNER_KINDS[ref.kind], name=ref.name, uid=ref.uid)

    if ref.kind == "ReplicaSet":
        replica_set = replicasets_by_uid.get(ref.uid)
        if replica_set is None:
            return None
        rs_ref = _controller_ref(replica_set.metadata.owner_references)
        if rs_ref is not None and rs_ref.kind == "Deployment":
            return ResolvedOwner(kind=WorkloadKind.DEPLOYMENT, name=rs_ref.name, uid=rs_ref.uid)
        # A ReplicaSet with no Deployment owner (created directly, not via a
        # Deployment) has no further recognized parent -- not a workload
        # kind this project models today.
        return None

    return None

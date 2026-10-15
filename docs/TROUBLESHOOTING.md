# Troubleshooting

Issues actually hit while building and verifying this project in its own dev
environment (kind 0.32 + kubectl 1.36 + Docker via Colima on Apple Silicon macOS), and
how they were resolved. Not a generic Kubernetes FAQ.

## `docker pull` / kind image pulls time out, even though the host machine has
internet access

**Symptom**: `docker pull busybox:1.36` (or any image) fails with
`dial tcp <ip>:443: i/o timeout` resolving `registry-1.docker.io`, while a plain
`curl https://registry-1.docker.io/v2/` from the host shell succeeds.

**Cause**: the Docker daemon (via Colima's VM) has its own network path, separate
from the host shell's. In this environment, that VM-level path couldn't reach Docker
Hub even though the host's direct network could. This is specific to the
Colima/Virtualization.framework networking setup, not a Kubernetes or kind issue.

**Fix**: don't depend on the cluster being able to pull from the internet. Use an
image already cached in the host's Docker image store (`docker images`), then load
it into the kind node's containerd directly:

```bash
docker save busybox:latest -o /tmp/busybox.tar
docker cp /tmp/busybox.tar kubesentinel-control-plane:/root/busybox.tar   # NOT /tmp -- see below
docker exec kubesentinel-control-plane ctr --namespace=k8s.io images import --digests --snapshotter=overlayfs /root/busybox.tar
```

Note: `kind load docker-image` and `kind load image-archive` both failed in this
environment with `ctr: content digest ... not found` -- apparently related to how
newer Docker's containerd-backed image store serializes multi-platform manifests.
The manual `docker cp` + `ctr images import` sequence above worked reliably.

## `docker cp ... /tmp/foo` silently reports success but the file isn't there

**Symptom**: `docker cp local.tar kubesentinel-control-plane:/tmp/foo.tar` exits 0,
but `docker exec kubesentinel-control-plane ls /tmp/` doesn't show it.

**Cause**: kind nodes run systemd, which mounts a fresh tmpfs over `/tmp` as part of
normal container boot; a copy landing there can be invisible or wiped depending on
timing relative to that mount.

**Fix**: copy to `/root/` (or another path not covered by a tmpfs mount) instead of
`/tmp/`.

## A Pod with `imagePullPolicy` unset keeps trying to pull even though the image
is already cached in the node

**Symptom**: `crictl images` on the kind node shows the image present, but the Pod
is stuck in `ErrImagePull`/`ImagePullBackOff` trying to pull it anyway.

**Cause**: Kubernetes defaults `imagePullPolicy` to `Always` specifically when the
image tag is `:latest` (or omitted). `Always` means the kubelet checks the registry
on every Pod start regardless of what's cached locally -- the local cache doesn't
exempt it.

**Fix**: set `imagePullPolicy: IfNotPresent` explicitly on any container using a
`:latest` tag (or any tag) when you want the local cache honored.

## A terminating Pod's container status can look transiently healthy

**Symptom**: deleting/replacing a Pod that was mid-incident caused the incident to
get marked `RESOLVED` even though the workload wasn't actually fixed -- the old Pod
was just being torn down.

**Cause**: a Pod with `metadata.deletionTimestamp` set can report container statuses
with no "bad" waiting/termination reason present for a brief window while it's being
torn down, which a health classifier reading only "is there a bad reason present"
would read as healthy.

**Fix**: `observer/service.py::ObserverService._handle_pod_event` now skips any Pod
with `deletionTimestamp` set entirely, before it ever reaches the incident manager.
See DESIGN_DECISIONS.md and TODO.md's "Known limitations" #2.

## kubelet reports OOM kills as generic `Error`/`exit_code=137`, not
`reason=OOMKilled`

**Symptom**: the `oom` lab scenario's Pod was genuinely OOM-killed (confirmed via the
node's own `cat /sys/fs/cgroup/memory.events`, which showed a nonzero `oom_kill`
counter), but `containerStatuses[].lastState.terminated.reason` read `"Error"`, not
`"OOMKilled"`.

**Cause**: not fully root-caused -- plausibly a kubelet/containerd/cgroup-v2-driver
interaction specific to this kind version on this host. Not assumed to be universal.

**Fix**: rather than relying solely on the explicit reason, `OOMKilledRule` also
recognizes `exit_code=137` with repeated restarts as a separate, lower-confidence,
explicitly inferred hypothesis. See DIAGNOSTIC_RULES.md.

## `kubernetes` Python client crashes the entire watch/list call on a zero-endpoint
EndpointSlice

**Symptom**: watching EndpointSlices (needed for `ServiceSelectorMismatchRule`)
crashed with `ValueError: Invalid value for 'endpoints', must not be 'None'` --
raised deep inside the client's own deserializer, which killed the *entire*
list/watch call (every EndpointSlice in the response), not just the offending one.

**Cause**: confirmed by fetching the raw JSON directly
(`discovery_api.list_namespaced_endpoint_slice(namespace=ns, _preload_content=False)`
then `json.loads(resp.data)`): the API server legitimately sends `"endpoints":
null` for a genuinely empty endpoint list -- this is just Go's `omitempty`
serializing a nil slice as `null` rather than `[]`. The generated
`V1EndpointSlice` model marks `endpoints` as a required list field and raises on
`None` instead of treating it as empty. This is exactly the case that matters most
for selector-mismatch detection (a Service matching zero Pods), so this wasn't a
rare edge case to shrug off.

**Fix**: bypass the client's typed deserialization for this one resource. For LIST,
call with `_preload_content=False` and parse the raw JSON directly instead of
letting the client build typed objects. For WATCH, construct
`kubernetes.watch.Watch(return_type="object")` -- the client's own
`unmarshal_event`/`__deserialize` maps the string `"object"` to Python's builtin
`object` type via `NATIVE_TYPES_MAPPING`, which short-circuits to a pass-through
(`__deserialize_object`) instead of strict model validation. Implemented once in
`observer/watchers.py::_ListWatchLoop` behind a `raw_object_watch=True` flag, so
only `EndpointSliceWatcher` pays for it. `endpointslice_evidence()` and
`ObserverService._handle_endpointslice_event` work on plain `dict`s as a
consequence -- the one place in this codebase where that's true instead of a typed
`kubernetes.client` model. Not yet re-checked against the client's newest release
(37.x as of this writing; installed version here is 30.1.0) -- plausible this is
fixed upstream by now, but the workaround is harmless either way. See TODO.md's
Known limitations #9.

## Every CLI command crashes with `TyperArgument.make_metavar() takes 1
positional argument but 2 were given`

**Symptom**: any `kubesentinel <command>` invocation crashes with that exact
`TypeError`, often while Click is trying to render an unrelated usage/error
message -- which made the first few occurrences look like a bug in the
command's own argument definitions, not a dependency problem.

**Cause**: `typer==0.12.5` declares only `click>=8.0.0` in its own metadata, no
upper bound, so pip was free to resolve the newest click (8.5.0) -- which
changed `click.Argument.make_metavar()`'s signature (added a required `ctx`
parameter). Typer 0.12.5's own `TyperArgument.make_metavar()` override predates
that change and doesn't match it. Confirmed with `pip show typer click` rather
than guessed.

**Fix**: pin `click==8.1.8` explicitly in `pyproject.toml` (already done) rather
than letting it float as a transitive dependency. If this surfaces again after a
`typer` upgrade, check whether the newer `typer` has widened or removed its own
click pin before assuming the same fix still applies.

## `kubesentinel dry-run`/`plan`'s `ACTION_TYPE` argument rejects the obvious
lowercase spelling

**Symptom**: `kubesentinel dry-run <incident-id> restart_deployment` fails with
`Invalid value for 'ACTION_TYPE:{RESTART_DEPLOYMENT|...}': 'restart_deployment'
is not one of 'RESTART_DEPLOYMENT', ...` -- found live while writing
Milestone 10's documentation pass, re-running every documented CLI command to
confirm it actually works: this exact lowercase form had been sitting in
README.md's own quickstart example, unexecuted, since Milestone 6.

**Cause**: the CLI argument's type is `ActionType` (a `str, Enum`); typer/click
matches a CLI argument against an `Enum`'s *members* by their literal value
string, not case-insensitively, and every `ActionType` value is uppercase
(`RESTART_DEPLOYMENT`, not `restart_deployment`).

**Fix**: use the uppercase enum value exactly as `kubesentinel dry-run --help`
prints it (`RESTART_DEPLOYMENT`, `ROLLBACK_DEPLOYMENT`, `SCALE_DEPLOYMENT`,
`RECOMMEND_RESOURCE_ADJUSTMENT`, `RECOMMEND_CONFIG_FIX`). README.md and
DEMO_SCRIPT.md were both corrected to match once this was caught by actually
running the commands instead of just writing them.

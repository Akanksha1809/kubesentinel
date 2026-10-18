"""Application configuration, loaded from environment variables / .env."""

from __future__ import annotations

from enum import Enum

from pydantic_settings import BaseSettings, SettingsConfigDict


class PolicyMode(str, Enum):
    """Controls whether remediation can ever be executed automatically.

    OBSERVE never executes or even proposes execution of actions.
    APPROVAL_REQUIRED prepares actions but requires an explicit API call to approve each one.
    CONTROLLED_AUTO executes, but only actions the policy allowlist explicitly permits.
    """

    OBSERVE = "OBSERVE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CONTROLLED_AUTO = "CONTROLLED_AUTO"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KUBESENTINEL_", env_file=".env", extra="ignore")

    kubeconfig: str | None = None
    kube_context: str | None = None

    # Comma-separated namespace list. Empty string means "all namespaces".
    watch_namespaces: str = ""
    action_allowed_namespaces: str = "kubesentinel-lab"
    # Optional additional restriction beyond namespace: specific Deployment names
    # remediation may ever target. Empty means "no restriction beyond namespace" --
    # the namespace allowlist is the mandatory gate; this is defense in depth for a
    # deployment that wants to scope remediation down to one or two named
    # workloads (e.g. "only ever touch the test Deployment, nothing else in the
    # namespace"), matching the spec's "approval-gated scaling of a specific test
    # Deployment."
    action_allowed_resource_names: str = ""

    # Node is cluster-scoped: watching it needs a ClusterRole, not just a namespaced
    # Role (see rbac/clusterrole.yaml). Default on since the lab grants it, but a
    # restricted deployment without cluster-scoped RBAC should set this to false
    # rather than let the Node watcher spin on permission-denied retries forever.
    watch_nodes: bool = True

    policy_mode: PolicyMode = PolicyMode.OBSERVE

    db_path: str = "./kubesentinel.db"

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_token: str = ""

    log_level: str = "INFO"

    # Resync period for the bounded list-based reconciliation safety net behind
    # watches. Also doubles as the upper bound on how long a genuinely-recovered,
    # now-idle Pod (no further watch events once it's stable) takes to get its
    # second confirmed-healthy observation -- see
    # incidents/manager.py::RECOVERY_CONFIRMATION_THRESHOLD. 60s matches typical
    # client-go informer resync cadence and keeps that wait reasonable without
    # approaching high-frequency polling.
    resync_interval_seconds: int = 60

    # Maximum remediation actions allowed per incident, and per rolling time window.
    max_actions_per_incident: int = 3
    max_actions_per_window: int = 10
    action_window_seconds: int = 600
    action_cooldown_seconds: int = 60

    # Hard timeout on any single mutating Kubernetes API call the remediation
    # executor makes (restart/rollback/scale). Keeps a slow/hung API server from
    # blocking the executor indefinitely instead of failing fast and recording
    # EXECUTION_FAILED.
    action_execution_timeout_seconds: int = 30

    # How long the recovery verifier polls after a successful execution before
    # giving up and recording INCONCLUSIVE rather than waiting indefinitely --
    # the spec's explicit "use timeouts rather than waiting indefinitely." 60s is
    # generous enough for a Deployment rollout to settle in the lab's small
    # workloads without blocking the caller for an unbounded time.
    verification_deadline_seconds: float = 60.0
    verification_poll_interval_seconds: float = 2.0

    # Optional AI summarization (Milestone 11). Disabled by default -- the spec
    # requires the project to work fully without any AI model, and for AI, when
    # enabled, to never determine authorization (see ai/summarizer.py and
    # RBAC_AND_THREAT_MODEL.md). The only backend implemented is a local Ollama
    # server: no API key, no cloud account, no per-request cost -- genuinely
    # free, not "free tier." A cloud provider could be added later behind the
    # same `AiClient` protocol without touching anything that calls it.
    ai_enabled: bool = False
    ai_base_url: str = "http://localhost:11434"
    ai_model: str = "qwen2.5-coder:3b"
    ai_timeout_seconds: float = 30.0

    # Resource optimization engine (read-only; see
    # docs/RESOURCE_OPTIMIZATION_DESIGN.md). Metrics-server access is always
    # attempted (no "enabled" flag -- a missing/unreachable metrics-server is
    # a normal, honestly-reported state, not a feature to toggle); Prometheus
    # is opt-in by setting its URL, mirroring `ai_enabled`'s "empty/unset
    # means off" pattern.
    optimization_metrics_timeout_seconds: float = 10.0
    optimization_prometheus_url: str = ""
    optimization_prometheus_timeout_seconds: float = 10.0
    # Bound on InMemorySampleStore's per-container ring buffer -- this is
    # process-memory, not disk, so it must stay small regardless of cluster
    # size. 60 samples at this project's typical resync-like cadence is a
    # deliberately modest "a little better than one snapshot," not an
    # attempt to replace real retained history (that's what Prometheus is for).
    optimization_sample_store_max_samples: int = 60
    # Namespaces excluded from optimization by default -- system namespaces
    # KubeSentinel has no business recommending changes for, regardless of
    # what their usage looks like. Still watched/inventoried (so a GET can
    # say "excluded: system namespace," not silence); never auto-flagged for
    # a resize.
    optimization_excluded_namespaces: str = "kube-system,kube-public,kube-node-lease,local-path-storage"

    # Analysis thresholds (optimization/analyzer.py, Milestone 4). Deliberately
    # conservative defaults -- the spec is explicit that this engine must not
    # recommend aggressive downsizing from thin evidence.
    #
    # A pooled sample count/window below these means EvidenceQuality can never
    # rise above SNAPSHOT_ONLY, and `proposed_*` stays None.
    optimization_min_samples_for_history: int = 3
    optimization_min_window_seconds_for_history: float = 300.0
    # CPU: proposed request = (a percentile of observed demand) * (1 + margin),
    # never below the floor. The percentile is deliberately *not* the bare
    # average -- see RESOURCE_OPTIMIZATION_DESIGN.md §7.
    optimization_cpu_typical_percentile: float = 0.95
    optimization_cpu_safety_margin: float = 0.2
    optimization_cpu_floor_millicores: int = 10
    # Memory: proposed request/limit is based on observed *peak* (or this high
    # percentile), never the average -- memory is not CPU; a transient average
    # is actively dangerous as the basis for a limit.
    optimization_memory_peak_percentile: float = 0.99
    optimization_memory_safety_margin: float = 0.15
    optimization_memory_floor_bytes: int = 16 * 1024 * 1024
    # A container is flagged "oversized" when its proposed request would be
    # below this fraction of its *current* request -- deliberately requires a
    # large, unambiguous gap, not "slightly less than requested."
    optimization_underutilization_ratio: float = 0.5
    # A container is flagged "near its limit" (pressure, not waste) once
    # observed usage reaches this fraction of the configured limit.
    optimization_near_limit_ratio: float = 0.85
    # How long a recommendation is considered fresh before a GET must say so
    # is EXPIRED rather than silently serving a stale number.
    optimization_recommendation_ttl_seconds: float = 86400.0

    @property
    def optimization_excluded_namespace_list(self) -> list[str]:
        return [n.strip() for n in self.optimization_excluded_namespaces.split(",") if n.strip()]

    # Capacity planning & autoscaling advisor (read-only; extends the
    # optimization engine above rather than a separate package -- see
    # docs/CAPACITY_PLANNING.md). Deliberately conservative defaults, same
    # discipline as the optimization_* thresholds above.
    #
    # A trend needs at least this many pooled samples, and the >=/<=
    # change ratio between the sampled window's first and second half, to
    # be called INCREASING/DECREASING rather than STABLE/INSUFFICIENT.
    capacity_trend_min_samples: int = 4
    capacity_trend_change_ratio: float = 0.2
    # A memory trend needs a window at least this long before the
    # leak-shaped pattern fires -- longer than optimization_min_window_
    # seconds_for_history, since a short window's "increasing" half-mean
    # comparison is far more likely to just be normal warm-up/traffic noise.
    capacity_leak_min_window_seconds: float = 900.0
    # The single-threaded-bottleneck pattern: a replica's CPU usage inside
    # this band (ceiling +/- band) is read as "plateaued near one core."
    capacity_single_core_ceiling_millicores: int = 1000
    capacity_single_core_band_millicores: int = 150
    # The pattern only fires once at least this many replicas independently
    # show the plateau (one replica alone proves nothing about *the code*
    # being single-threaded -- it could just be that pod's own real load).
    capacity_min_replicas_for_bottleneck_check: int = 2
    # ...and only when the configured CPU limit allows meaningfully more
    # than one core -- a container limited to ~1 core plateauing at ~1 core
    # is just "hit its limit," not evidence of a single-threaded ceiling.
    capacity_min_limit_ratio_for_bottleneck_check: float = 1.5

    # Security & configuration auditor (read-only; see
    # docs/SECURITY_AUDITING.md). Namespace exclusion reuses
    # optimization_excluded_namespace_list directly -- never a second,
    # possibly-diverging exclusion list for the same system namespaces.
    #
    # Kubernetes' own built-in, intentionally-broad ClusterRoles -- never
    # flagged by WILDCARD_RBAC_RULE (there is no fix a cluster operator
    # can apply to a built-in Role). Anything with a "system:" prefix is
    # excluded structurally (not configurable), since that's a stable
    # Kubernetes naming convention, not a threshold to tune.
    security_builtin_wildcard_role_names: str = "cluster-admin,admin,edit,view"
    # CLUSTER_ADMIN_BINDING: a Group subject whose name starts with one of
    # these prefixes is one of Kubernetes' own bootstrap identities, never
    # flagged -- confirmed live against this project's own kind cluster to
    # need *two* prefixes, not one: `system:masters` (the universal
    # Kubernetes bootstrap Group) and `kubeadm:cluster-admins` (kubeadm's
    # own, used by admin.conf). A ServiceAccount or User subject is never
    # exempted by this list, no matter its name -- only a cluster's own
    # built-in Group identities are.
    security_builtin_subject_group_prefixes: str = "system:,kubeadm:"
    # MISSING_PDB_FOR_MULTI_REPLICA_WORKLOAD only applies once a workload
    # has at least this many desired replicas -- a single-replica workload
    # has no "voluntary disruption while staying available" concern a PDB
    # would address.
    security_min_replicas_for_pdb_check: int = 2

    # CVE/image vulnerability scanning (docs/SECURITY_AUDITING.md §14) --
    # opt-in, off by default: unlike every other check in this engine, it
    # shells out to a local Trivy binary rather than reading an
    # already-typed Kubernetes object, so it's gated the same way
    # ai_enabled/optimization_prometheus_url already gate this project's
    # other genuinely optional extras. False means zero code runs and
    # zero subprocess calls happen -- not "best-effort," identical to this
    # feature not existing.
    security_enable_image_scanning: bool = False
    # Passed straight to `trivy image --severity <this>` -- Trivy does its
    # own floor filtering (more efficient than fetch-then-filter in
    # Python, and keeps findings actionable rather than flooding
    # `security-findings` with the dozens of LOW/MEDIUM CVEs a typical
    # base image carries).
    security_image_scan_min_severity: str = "HIGH,CRITICAL"
    # Generous because Trivy's first scan on a machine can include
    # downloading its own vulnerability database -- a one-time cost this
    # project does not pre-warm or special-case.
    security_image_scan_timeout_seconds: int = 180
    # Caps the total number of distinct images one audit() call will run
    # trivy against -- each call is already timeout-bounded, but a
    # cluster with many distinct images could otherwise make a single
    # security-audit call run for an unbounded total duration.
    security_image_scan_max_images: int = 25

    # Active connectivity probe (docs/NETWORK_DIAGNOSTICS.md §12) -- this
    # project's first new mutation capability outside the fully
    # policy-gated RemediationExecutor. Off by default, same "genuinely
    # optional" guarantee ai_enabled/security_enable_image_scanning
    # already established: False means network/probe.py never runs, zero
    # new API calls happen, identical to this capability not existing.
    network_probe_enabled: bool = False
    # Deliberately its own, separate allowlist from action_allowed_namespaces
    # -- conflating the two would wrongly imply this feature and
    # remediation share a risk model. They don't.
    network_probe_allowed_namespaces: str = "kubesentinel-lab"
    # Per (namespace, service_name, port) -- prevents hammering the same
    # target repeatedly.
    network_probe_cooldown_seconds: int = 30
    # Counted by listing Jobs carrying the kubesentinel.io/probe=true label
    # in the target namespace that haven't finished yet.
    network_probe_max_concurrent_per_namespace: int = 1
    # Job.spec.activeDeadlineSeconds -- the probe Job can never run longer
    # than this, no matter what the script inside it does.
    network_probe_job_timeout_seconds: int = 15
    # The `nc -w` timeout inside the probe script itself -- kept well
    # under network_probe_job_timeout_seconds so the script's own TCP
    # attempt can never be what causes the Job-level deadline to fire.
    network_probe_tcp_timeout_seconds: int = 5
    # Job.spec.ttlSecondsAfterFinished -- a backstop that garbage-collects
    # the Job (and its Pod) even if this project's own explicit deletion
    # after reading the result never runs at all.
    network_probe_job_ttl_seconds: int = 60
    # How long NetworkService.run_probe waits for the Job to reach a
    # terminal state before giving up and reporting a timeout -- always
    # longer than network_probe_job_timeout_seconds, with margin for Pod
    # scheduling/startup.
    network_probe_poll_timeout_seconds: float = 20.0
    network_probe_poll_interval_seconds: float = 1.0
    # Bounds each individual Kubernetes API call `run_probe` makes
    # (read_namespaced_service, create/read-status/delete on the Job,
    # list/read-log on its Pod) -- matching RemediationExecutor's own
    # action_execution_timeout_seconds precedent. Without this, a single
    # slow/hung apiserver response can block far longer than
    # network_probe_poll_timeout_seconds itself, since that deadline is
    # only checked *between* polling calls, never during one -- caught
    # live, where an unbounded call left a probe hanging over 20 minutes
    # against a genuinely overloaded apiserver.
    network_probe_api_timeout_seconds: float = 10.0
    # The probe's own DNS check queries the Service's full in-namespace
    # FQDN (<service>.<namespace>.svc.<this>), never the bare short name
    # -- caught live: BusyBox's nslookup, resolving a bare name through
    # resolv.conf's search-domain list, returns a *nonzero* exit code
    # even when a later search domain resolves successfully, making the
    # script's own `&&`-gated success marker unreliable. The fully
    # qualified name resolves in one clean attempt instead. Configurable
    # rather than hardcoded for a cluster with a non-default
    # --cluster-domain, though "cluster.local" covers every real cluster
    # this project runs against today.
    network_probe_cluster_domain: str = "cluster.local"

    @property
    def security_builtin_subject_group_prefix_list(self) -> list[str]:
        return [p.strip() for p in self.security_builtin_subject_group_prefixes.split(",") if p.strip()]

    @property
    def security_builtin_wildcard_role_name_list(self) -> list[str]:
        return [n.strip() for n in self.security_builtin_wildcard_role_names.split(",") if n.strip()]

    @property
    def watch_namespace_list(self) -> list[str]:
        return [n.strip() for n in self.watch_namespaces.split(",") if n.strip()]

    @property
    def action_allowed_namespace_list(self) -> list[str]:
        return [n.strip() for n in self.action_allowed_namespaces.split(",") if n.strip()]

    @property
    def action_allowed_resource_name_list(self) -> list[str]:
        return [n.strip() for n in self.action_allowed_resource_names.split(",") if n.strip()]

    @property
    def network_probe_allowed_namespace_list(self) -> list[str]:
        return [n.strip() for n in self.network_probe_allowed_namespaces.split(",") if n.strip()]


def get_settings() -> Settings:
    return Settings()

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

    @property
    def watch_namespace_list(self) -> list[str]:
        return [n.strip() for n in self.watch_namespaces.split(",") if n.strip()]

    @property
    def action_allowed_namespace_list(self) -> list[str]:
        return [n.strip() for n in self.action_allowed_namespaces.split(",") if n.strip()]

    @property
    def action_allowed_resource_name_list(self) -> list[str]:
        return [n.strip() for n in self.action_allowed_resource_names.split(",") if n.strip()]


def get_settings() -> Settings:
    return Settings()

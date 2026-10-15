"""KubeSentinel CLI.

A thin client over the same observer/diagnostic/persistence code the API uses --
every command here does one-shot work against the live cluster and/or the local
SQLite store, with no long-running watch loop (that's the API server's job).
"""

from __future__ import annotations

import json as json_module

import typer
from rich.console import Console
from rich.table import Table

from kubesentinel.ai.client import AiClientError, OllamaClient
from kubesentinel.ai.summarizer import summarize_incident
from kubesentinel.config import Settings, get_settings
from kubesentinel.diagnostics.engine import DiagnosticEngine
from kubesentinel.logging_config import configure_logging
from kubesentinel.models.action import ActionType
from kubesentinel.observer.client import build_clients
from kubesentinel.observer.inspector import pod_evidence
from kubesentinel.persistence.sqlite_repo import SQLiteIncidentRepository
from kubesentinel.policy.engine import PolicyEngine
from kubesentinel.remediation.executor import RemediationExecutor
from kubesentinel.remediation.service import (
    ActionNotFoundError,
    InvalidActionStateError,
    RemediationService,
)
from kubesentinel.verification.verifier import RecoveryVerifier

app = typer.Typer(help="KubeSentinel: Kubernetes troubleshooting and controlled self-healing.")
console = Console()


@app.command("inspect")
def inspect_namespace(namespace: str = typer.Argument(..., help="Namespace to inspect")) -> None:
    """List pods in a namespace with their phase and container state summary."""
    settings = get_settings()
    configure_logging(settings.log_level)
    clients = build_clients(settings)
    pods = clients.core.list_namespaced_pod(namespace=namespace).items

    table = Table(title=f"Pods in {namespace}")
    table.add_column("Name")
    table.add_column("Phase")
    table.add_column("Restarts")
    table.add_column("Containers")
    for pod in pods:
        restarts = sum((cs.restart_count for cs in pod.status.container_statuses or []), 0)
        container_summary = ", ".join(
            f"{cs.name}:{_state_label(cs)}" for cs in (pod.status.container_statuses or [])
        )
        table.add_row(pod.metadata.name, pod.status.phase or "Unknown", str(restarts), container_summary)
    console.print(table)


@app.command("diagnose")
def diagnose(
    namespace: str = typer.Argument(..., help="Namespace of the pod"),
    pod_name: str = typer.Argument(..., help="Pod name to diagnose"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Diagnose a single pod directly against the live cluster (no persistence)."""
    settings = get_settings()
    configure_logging(settings.log_level)
    clients = build_clients(settings)
    pod = clients.core.read_namespaced_pod(name=pod_name, namespace=namespace)

    evidence = pod_evidence(pod)
    result = DiagnosticEngine().diagnose(evidence)

    if json_output:
        console.print(json_module.dumps(result.model_dump(mode="json"), indent=2))
        return

    if not result.hypotheses:
        console.print(f"[yellow]No diagnostic rule matched for pod '{pod_name}'. Inconclusive.[/yellow]")
        return

    for h in result.hypotheses:
        console.print(f"[bold]{h.reason_code.value}[/bold] (confidence: {h.confidence.value})")
        console.print(f"  {h.explanation}")
        if h.recommended_next_steps:
            console.print("  Next steps:")
            for step in h.recommended_next_steps:
                console.print(f"    - {step}")
        console.print()


@app.command("incidents")
def list_incidents(
    status: str = typer.Option(None, help="Filter by status, e.g. OPEN, RESOLVED"),
    namespace: str = typer.Option(None, help="Filter by namespace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List incidents recorded in the local store."""
    settings = get_settings()
    repo = SQLiteIncidentRepository(settings.db_path)
    incidents = repo.list(status=status, namespace=namespace)

    if json_output:
        console.print(json_module.dumps([i.model_dump(mode="json") for i in incidents], indent=2))
        return

    table = Table(title="Incidents")
    for col in ("ID", "Namespace", "Resource", "Category", "Severity", "Status", "First Observed"):
        table.add_column(col)
    for i in incidents:
        table.add_row(
            i.incident_id[:8],
            i.resource.namespace,
            i.resource.name,
            i.category,
            i.severity.value,
            i.status.value,
            i.first_observed_at.isoformat(),
        )
    console.print(table)


@app.command("evidence")
def show_evidence(incident_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    """Print the evidence collected for one incident."""
    settings = get_settings()
    repo = SQLiteIncidentRepository(settings.db_path)
    incident = repo.get(incident_id)
    if incident is None:
        console.print(f"[red]No incident found with id {incident_id}[/red]")
        raise typer.Exit(code=1)

    if json_output:
        console.print(json_module.dumps([e.model_dump(mode="json") for e in incident.evidence], indent=2))
        return

    for e in incident.evidence:
        console.print(f"[{e.kind.value}] {e.summary}")


@app.command("audit")
def show_audit(incident_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    """Print the audit log for one incident."""
    settings = get_settings()
    repo = SQLiteIncidentRepository(settings.db_path)
    incident = repo.get(incident_id)
    if incident is None:
        console.print(f"[red]No incident found with id {incident_id}[/red]")
        raise typer.Exit(code=1)

    if json_output:
        console.print(json_module.dumps([a.model_dump(mode="json") for a in incident.audit_log], indent=2))
        return

    for a in incident.audit_log:
        console.print(f"{a.timestamp.isoformat()} [{a.actor}] {a.event_type} {a.details}")


@app.command("explain")
def explain_incident(
    incident_id: str = typer.Argument(..., help="Incident to summarize"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Ask a local AI model to summarize this incident's existing diagnosis in
    plain English. Advisory only -- adds no evidence, changes no diagnosis,
    and cannot approve or trigger anything. Requires KUBESENTINEL_AI_ENABLED=true
    and a local Ollama server (see .env.example) -- there is no cloud/paid
    backend in this project."""
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.ai_enabled:
        console.print(
            "[red]AI summarization is disabled.[/red] Set KUBESENTINEL_AI_ENABLED=true "
            "(and run a local Ollama server) to use this command."
        )
        raise typer.Exit(code=1)

    repo = SQLiteIncidentRepository(settings.db_path)
    incident = _load_incident_or_exit(repo, incident_id)
    client = OllamaClient(settings.ai_base_url, settings.ai_model, settings.ai_timeout_seconds)
    try:
        summary = summarize_incident(incident, client, settings.ai_model)
    except AiClientError as exc:
        console.print(f"[red]AI backend unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print(json_module.dumps(summary.model_dump(mode="json"), indent=2))
        return
    console.print(summary.text)
    console.print(f"\n[dim]{summary.disclaimer}[/dim]")


def _build_remediation_service(settings: Settings) -> tuple[RemediationService, SQLiteIncidentRepository]:
    clients = build_clients(settings)
    repo = SQLiteIncidentRepository(settings.db_path)
    executor = RemediationExecutor(clients.apps, timeout_seconds=settings.action_execution_timeout_seconds)
    verifier = RecoveryVerifier(
        clients.apps,
        deadline_seconds=settings.verification_deadline_seconds,
        poll_interval_seconds=settings.verification_poll_interval_seconds,
    )
    return RemediationService(PolicyEngine(settings), executor, verifier, repo, settings), repo


def _load_incident_or_exit(repo: SQLiteIncidentRepository, incident_id: str):
    incident = repo.get(incident_id)
    if incident is None:
        console.print(f"[red]No incident found with id {incident_id}[/red]")
        raise typer.Exit(code=1)
    return incident


def _build_parameters(replicas: int | None, revision: int | None, recommendation: str | None) -> dict:
    parameters: dict = {}
    if replicas is not None:
        parameters["replicas"] = replicas
    if revision is not None:
        parameters["revision"] = revision
    if recommendation is not None:
        parameters["recommendation"] = recommendation
    return parameters


@app.command("dry-run")
def dry_run_action(
    incident_id: str = typer.Argument(..., help="Incident to target"),
    action_type: ActionType = typer.Argument(..., help="Action type to preview"),
    target_replicas: int = typer.Option(None, "--target-replicas", help="Target's current desired replica count"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Preview the policy decision for an action with zero side effects -- no
    action is created, persisted, or executed."""
    settings = get_settings()
    configure_logging(settings.log_level)
    service, repo = _build_remediation_service(settings)
    incident = _load_incident_or_exit(repo, incident_id)

    decision = service.dry_run(incident, action_type, target_desired_replicas=target_replicas)

    if json_output:
        console.print(json_module.dumps({"outcome": decision.outcome.value, "reason": decision.reason}, indent=2))
        return
    console.print(f"[bold]{decision.outcome.value}[/bold]: {decision.reason}")


@app.command("plan")
def plan_action(
    incident_id: str = typer.Argument(..., help="Incident to target"),
    action_type: ActionType = typer.Argument(..., help="Action type to propose"),
    rationale: str = typer.Option(..., "--rationale", help="Why this action is being proposed"),
    replicas: int = typer.Option(None, "--replicas", help="Target replica count (SCALE_DEPLOYMENT)"),
    revision: int = typer.Option(None, "--revision", help="Target revision (ROLLBACK_DEPLOYMENT)"),
    recommendation: str = typer.Option(None, "--recommendation", help="Advisory text (RECOMMEND_* types)"),
    target_replicas: int = typer.Option(None, "--target-replicas", help="Target's current desired replica count"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Propose a remediation action and evaluate it against policy. May execute
    immediately if CONTROLLED_AUTO allows it unattended; otherwise leaves it
    PENDING_APPROVAL for a separate `approve` + `execute` call."""
    settings = get_settings()
    configure_logging(settings.log_level)
    service, repo = _build_remediation_service(settings)
    incident = _load_incident_or_exit(repo, incident_id)
    parameters = _build_parameters(replicas, revision, recommendation)

    action = service.propose(incident, action_type, rationale, parameters, target_replicas)

    if json_output:
        console.print(json_module.dumps(action.model_dump(mode="json"), indent=2))
        return
    console.print(f"action_id: {action.action_id}")
    console.print(f"status: [bold]{action.status.value}[/bold]")
    if action.execution_error:
        console.print(f"error: {action.execution_error}")
    if action.verification:
        console.print(f"verification: {action.verification.status.value} -- {action.verification.reason}")


@app.command("approve")
def approve_action(
    incident_id: str = typer.Argument(...),
    action_id: str = typer.Argument(...),
    approved_by: str = typer.Option(..., "--approved-by"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Approve a PENDING_APPROVAL action. Does not execute it -- run `execute` next."""
    settings = get_settings()
    configure_logging(settings.log_level)
    service, repo = _build_remediation_service(settings)
    incident = _load_incident_or_exit(repo, incident_id)

    try:
        action = service.approve(incident, action_id, approved_by)
    except (ActionNotFoundError, InvalidActionStateError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print(json_module.dumps(action.model_dump(mode="json"), indent=2))
        return
    console.print(f"status: [bold]{action.status.value}[/bold]")


@app.command("execute")
def execute_action(
    incident_id: str = typer.Argument(...),
    action_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Execute an APPROVED action and verify the outcome. Blocks up to the
    verification deadline."""
    settings = get_settings()
    configure_logging(settings.log_level)
    service, repo = _build_remediation_service(settings)
    incident = _load_incident_or_exit(repo, incident_id)

    try:
        action = service.execute(incident, action_id)
    except (ActionNotFoundError, InvalidActionStateError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print(json_module.dumps(action.model_dump(mode="json"), indent=2))
        return
    console.print(f"status: [bold]{action.status.value}[/bold]")
    if action.execution_error:
        console.print(f"error: {action.execution_error}")
    if action.verification:
        console.print(f"verification: {action.verification.status.value} -- {action.verification.reason}")


@app.command("reject")
def reject_action(
    incident_id: str = typer.Argument(...),
    action_id: str = typer.Argument(...),
    rejected_by: str = typer.Option(..., "--rejected-by"),
    reason: str = typer.Option(..., "--reason"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reject a PENDING_APPROVAL action."""
    settings = get_settings()
    configure_logging(settings.log_level)
    service, repo = _build_remediation_service(settings)
    incident = _load_incident_or_exit(repo, incident_id)

    try:
        action = service.reject(incident, action_id, rejected_by, reason)
    except (ActionNotFoundError, InvalidActionStateError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print(json_module.dumps(action.model_dump(mode="json"), indent=2))
        return
    console.print(f"status: [bold]{action.status.value}[/bold]")


def _state_label(cs) -> str:
    if cs.state.waiting:
        return f"Waiting({cs.state.waiting.reason})"
    if cs.state.terminated:
        return f"Terminated({cs.state.terminated.reason})"
    if cs.state.running:
        return "Running"
    return "Unknown"


if __name__ == "__main__":
    app()

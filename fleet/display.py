import json
from rich.console import Console
from rich.table import Table
from fleet.models import FleetState, PauseResult, SourceHealth
from fleet.pr_report import PRReport

console = Console()

def render_fleet_state(state: FleetState, as_json: bool = False):
    if as_json:
        console.print(state.model_dump_json(indent=2))
        return

    table = Table(title=f"Fleet State (Collected at {state.collected_at.isoformat()})")
    table.add_column("Deployment", style="bold cyan")
    table.add_column("Service", style="bold")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Essential")
    table.add_column("Uptime")
    table.add_column("Image")
    table.add_column("Diag")

    # Group by deployment
    from collections import defaultdict
    grouped = defaultdict(list)
    for svc in state.services:
        dep = svc.deployment or "unknown"
        grouped[dep].append(svc)

    # Sort deployments alphabetically
    for dep in sorted(grouped.keys()):
        first_row = True
        for svc in grouped[dep]:
            status_color = "dim"
            if svc.status == "running": status_color = "green"
            elif svc.status == "stopped": status_color = "red"
            elif svc.status == "paused": status_color = "yellow"
            elif svc.status == "error": status_color = "bold red"
            elif svc.status == "routed": status_color = "cyan"
    
            status_text = f"[{status_color}]{svc.status}[/{status_color}]"
            essential_text = "[green]✓[/green]" if svc.essential else "[dim]—[/dim]"
            
            diag_text = "[dim]—[/dim]"
            if svc.diagnostics:
                issues = []
                for k, v in svc.diagnostics.items():
                    if v: issues.append(k)
                if issues:
                    diag_text = f"[yellow]⚠ {', '.join(issues)}[/yellow]"
                else:
                    diag_text = "[green]✓[/green]"
    
            table.add_row(
                dep if first_row else "",
                svc.name,
                svc.source,
                status_text,
                essential_text,
                svc.uptime or "[dim]—[/dim]",
                svc.image or "[dim]—[/dim]",
                diag_text
            )
            first_row = False

    console.print(table)
    
    # Footer
    footer = []
    for s in state.sources:
        if s.reachable:
            footer.append(f"{s.name} [green]✓[/green]")
        else:
            footer.append(f"{s.name} [red]✗[/red] ({s.error})")
    
    console.print(f"Sources: {'  '.join(footer)}")

    if not any(s.reachable for s in state.sources):
        console.print("\n[bold red]🚨 CRITICAL: FLEET IS COMPLETELY DOWN (POST-METEOR STATE) 🚨[/bold red]")
        console.print("[yellow]It is 3AM. The NOC woke you up. Here is exactly what to do to recover the fleet:[/yellow]")
        console.print("\n[bold]1. Clone essential control-plane repositories:[/bold]")
        console.print("   [dim]git clone https://github.com/your-org/traefik ~/work/traefik[/dim]")
        console.print("   [dim]git clone https://github.com/your-org/delightd ~/work/delightd[/dim]")
        console.print("\n[bold]2. Bootstrap the network mesh & control plane:[/bold]")
        console.print("   [dim]cd ~/work/traefik && docker compose up -d[/dim]")
        console.print("   [dim]cd ~/work/delightd && go build ./cmd/delightd && ./delightd &[/dim]")
        console.print("\n[bold]3. Resume normal operations:[/bold]")
        console.print("   [dim]The routing layer will automatically reconstruct via delightd.[/dim]")
        console.print("   [dim]Run `fleet show` again once the control plane is online.[/dim]\n")

    # LLM Telemetry
    total_llm_time = sum(svc.diagnostics.get("llm_time_ms", 0) for svc in state.services if svc.diagnostics)
    total_tokens = sum(svc.diagnostics.get("llm_tokens", 0) for svc in state.services if svc.diagnostics)
    
    if total_llm_time > 0 or total_tokens > 0:
        console.print(f"[dim]LLM Analysis Time: {total_llm_time}ms | Tokens Burned: {total_tokens}[/dim]")


def render_pause_result(result: PauseResult):
    if result.dry_run:
        console.print("[yellow bold]DRY RUN — no changes made[/yellow bold]")
    
    table = Table(title=f"Pause Operation: {result.action.upper()}")
    table.add_column("Service")
    table.add_column("Status")
    table.add_column("Message")

    for svc in result.affected:
        table.add_row(svc.name, "[green]Affected[/green]", f"{result.action} applied")
        
    for svc in result.skipped:
        table.add_row(svc.name, "[dim]Skipped[/dim]", "Essential service")
        
    for err in result.errors:
        table.add_row(err["name"], "[red]Error[/red]", err["error"])
        
    console.print(table)


_PR_CLASS_STYLE = {
    "needs-review": "yellow",
    "ready-to-land": "green",
    "stacked-approve-only": "cyan",
    "blocked": "red",
}


def render_pr_report(report: PRReport):
    # human view of the pr survey. one table per repo; the classification column
    # is colour-keyed and a blocked/stacked pr names the pr it waits on.
    if not report.repos:
        console.print("[dim]No GitHub roster repositories found.[/dim]")
        return

    for repo in report.repos:
        title = f"{repo.name} ({repo.slug})"
        table = Table(title=title)
        table.add_column("PR", justify="right", style="bold")
        table.add_column("Title")
        table.add_column("Base")
        table.add_column("Classification")
        table.add_column("Waits On")

        if repo.error:
            console.print(f"[red]✗ {title}: {repo.error}[/red]")
            continue

        if not repo.open_prs:
            console.print(f"[dim]{title}: no open PRs[/dim]")
            continue

        for pr in repo.open_prs:
            style = _PR_CLASS_STYLE.get(pr.classification, "white")
            classification = f"[{style}]{pr.classification}[/{style}]"
            waits = f"#{pr.blocked_on}" if pr.blocked_on else "[dim]—[/dim]"
            table.add_row(
                f"#{pr.number}",
                pr.title,
                pr.base,
                classification,
                waits,
            )

        console.print(table)


def render_selfcheck(sources: list[SourceHealth]):
    table = Table(title="Selfcheck: Source Reachability")
    table.add_column("Source")
    table.add_column("Reachable")
    table.add_column("Latency (ms)")
    table.add_column("Error")

    for s in sources:
        reachable_text = "[green]True[/green]" if s.reachable else "[red]False[/red]"
        latency_text = f"{s.latency_ms:.1f}" if s.latency_ms is not None else "[dim]—[/dim]"
        error_text = s.error or "[dim]—[/dim]"
        
        table.add_row(s.name, reachable_text, latency_text, error_text)

    console.print(table)

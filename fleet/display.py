import json
from rich.console import Console
from rich.table import Table
from fleet.models import FleetState, PauseResult, SourceHealth

console = Console()

def render_fleet_state(state: FleetState, as_json: bool = False):
    if as_json:
        console.print(state.model_dump_json(indent=2))
        return

    table = Table(title=f"Fleet State (Collected at {state.collected_at.isoformat()})")
    table.add_column("Service", style="bold")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Essential")
    table.add_column("Uptime")
    table.add_column("Image")
    table.add_column("Diag")

    for svc in state.services:
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
            svc.name,
            svc.source,
            status_text,
            essential_text,
            svc.uptime or "[dim]—[/dim]",
            svc.image or "[dim]—[/dim]",
            diag_text
        )

    console.print(table)
    
    # Footer
    footer = []
    for s in state.sources:
        if s.reachable:
            footer.append(f"{s.name} [green]✓[/green]")
        else:
            footer.append(f"{s.name} [red]✗ ({s.error})[/red]")
    
    console.print(f"Sources: {'  '.join(footer)}")


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

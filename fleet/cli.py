import click
import asyncio
from fleet.core import FleetCore
from fleet.display import render_fleet_state, render_pause_result, render_selfcheck

import functools

def coro(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper

@click.group(invoke_without_command=True)
@click.pass_context
@coro
async def main(ctx):
    if ctx.invoked_subcommand is None:
        core = FleetCore()
        state = await core.show()
        render_fleet_state(state)

@main.command()
@click.argument('status_filter', required=False, type=click.Choice(['healthy', 'unhealthy', 'questionable']))
@click.option('--source', default=None, help='Filter by source')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON')
@coro
async def show(status_filter, source, as_json):
    core = FleetCore()
    state = await core.show(source_filter=source, status_filter=status_filter)
    render_fleet_state(state, as_json=as_json)

@main.command()
@click.option('--dry-run', is_flag=True, help='Preview without acting')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
@coro
async def pause(dry_run, yes):
    if not dry_run and not yes:
        click.confirm("Are you sure you want to pause non-essential services?", abort=True)
    
    core = FleetCore()
    result = await core.pause(dry_run=dry_run)
    render_pause_result(result)

@main.command()
@click.option('--dry-run', is_flag=True, help='Preview without acting')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
@coro
async def resume(dry_run, yes):
    if not dry_run and not yes:
        click.confirm("Are you sure you want to resume paused services?", abort=True)
        
    core = FleetCore()
    result = await core.resume(dry_run=dry_run)
    render_pause_result(result)

@main.command()
@coro
async def selfcheck():
    core = FleetCore()
    sources_health = await core.selfcheck()
    render_selfcheck(sources_health)

@main.command()
@click.option('--port', default=9400, help='HTTP server port')
def serve(port):
    import uvicorn
    from fleet.server import create_app
    from fleet.auth import SSHAuthenticator
    
    core = FleetCore()
    auth = SSHAuthenticator()
    app = create_app(core, auth)
    
    uvicorn.run(app, host="0.0.0.0", port=port)

@main.group()
def models():
    """Manage and inspect dynamically discovered local LLMs."""
    pass

@models.command(name='ls')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON')
@coro
async def models_ls(as_json):
    """List all LLM models discovered by the control plane."""
    core = FleetCore()
    sources = await core.models()
    
    if as_json:
        import json
        click.echo(json.dumps(sources, indent=2))
        return
        
    if not sources:
        click.echo("No local LLM sources discovered.")
        return
        
    for source in sources:
        status = click.style("Healthy", fg="green") if source.get("healthy") else click.style("Offline", fg="red")
        click.echo(f"🧠 {click.style(source.get('provider', 'unknown'), bold=True)} ({source.get('url')}) - {status}")
        models = source.get("models", [])
        if models:
            for model in models:
                click.echo(f"   └─ {model}")
        else:
            click.echo("   └─ (no models loaded)")
        click.echo()

@main.command()
@click.argument('config_file', default='WorkstationConfig.yaml')
@click.option('--dry-run', is_flag=True, default=True, help='Always dry-run for now')
@coro
async def apply(config_file, dry_run):
    """Apply the declarative WorkstationConfig to the local machine."""
    import yaml
    from fleet.models import WorkstationConfig
    
    try:
        with open(config_file, 'r') as f:
            data = yaml.safe_load(f)
        config = WorkstationConfig(**data)
    except Exception as e:
        click.echo(click.style(f"Error parsing config: {e}", fg="red"))
        return

    click.echo(click.style(f"Applying WorkstationConfig v{config.version} (Dry-Run)", bold=True, fg="cyan"))
    
    click.echo("\n[Phase 1: Foundation]")
    for daemon in config.host.daemons:
        click.echo(f" + Verify daemon: {daemon}")
        
    click.echo("\n[Phase 2: Repositories]")
    for repo in config.repositories:
        click.echo(f" + git clone {repo.origin} {repo.path}")

    click.echo("\n[Phase 3: Models]")
    for model in config.models:
        if model.provider == "huggingface":
            click.echo(f" + huggingface-cli download {model.id} {model.file or ''}")
        elif model.provider == "ollama":
            click.echo(f" + ollama pull {model.id}")
            
    click.echo("\n[Phase 4: Mesh Boot]")
    for repo in filter(lambda r: r.essential, config.repositories):
        click.echo(f" + cd {repo.path} && docker compose up -d")

@main.command()
@coro
async def sync():
    """Ensure no repositories are dirty before teardown."""
    import httpx
    
    click.echo("Checking workstation git state via transparent...")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # We fetch data.json from transparent, but transparent might not expose it directly via API
            # For now, we simulate fetching it from disk since transparent writes it to ~/work/transparent/REPORT/data.json
            import json, os
            report_path = os.path.expanduser("~/work/transparent/REPORT/data.json")
            if not os.path.exists(report_path):
                click.echo(click.style("transparent report not found. Cannot verify dirty state safely.", fg="red"))
                return
                
            with open(report_path, "r") as f:
                data = json.load(f)
                
            dirty = []
            unpushed = []
            for repo in data.get("Repos", []):
                if repo.get("Dirty"):
                    dirty.append(repo["Name"])
                if repo.get("Unpushed", 0) > 0:
                    unpushed.append(repo["Name"])
                    
            if dirty or unpushed:
                click.echo(click.style("\n🚨 BLOCKED: Workstation has uncommitted or unpushed state!", fg="red", bold=True))
                if dirty:
                    click.echo(f"Dirty repositories: {', '.join(dirty)}")
                if unpushed:
                    click.echo(f"Unpushed repositories: {', '.join(unpushed)}")
                click.echo("\nPlease commit and push all changes before attempting a host migration or teardown.")
                import sys; sys.exit(1)
                
            click.echo(click.style("✓ Workstation is clean and safe to teardown.", fg="green"))
            
    except Exception as e:
        click.echo(click.style(f"Error checking sync state: {e}", fg="red"))

if __name__ == "__main__":
    main()

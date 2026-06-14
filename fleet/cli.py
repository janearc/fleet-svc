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

    if config.host.packages or config.host.casks:
        click.echo("\n[Phase 1.5: Dev Environment (Brewfile)]")
        click.echo(" + Generating ephemeral /tmp/Brewfile")
        for pkg in config.host.packages:
            click.echo(f"   brew \"{pkg}\"")
        for cask in config.host.casks:
            click.echo(f"   cask \"{cask}\"")
        click.echo(" + brew bundle --file=/tmp/Brewfile")
        
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

@main.command()
@click.option('--dry-run', is_flag=True, help='Preview what will be started without taking action')
@click.option('--force', is_flag=True, help='Force rebuild of binaries even if they exist')
@click.option('--dirty', is_flag=True, help='Allow building from dirty repositories')
@coro
async def bootstrap(dry_run, force, dirty):
    """Ignite the fleet from a cold boot: check daemons and start tier-0 services."""
    import subprocess
    import os
    
    title = "🚀 Bootstrapping Fleet Control Plane"
    if dry_run: title += " (DRY RUN)"
    click.echo(click.style(title, bold=True, fg="cyan"))
    
    core = FleetCore()
    sources_health = await core.selfcheck()
    
    docker_health = next((s for s in sources_health if s.name == "docker"), None)
    kube_health = next((s for s in sources_health if s.name == "kube"), None)
    
    docker_running = docker_health and docker_health.reachable
    kube_running = kube_health and kube_health.reachable
    
    click.echo("\n[Phase 1: Checking Host Daemons]")
    if docker_running:
        click.echo(click.style(" ✓ Docker is running.", fg="green"))
    else:
        if dry_run:
            click.echo(click.style(" ✗ Docker is NOT running. Would attempt to start Docker via 'open -a Docker'.", fg="yellow"))
        else:
            click.echo(click.style(" ✗ Docker is NOT running. Attempting to start Docker...", fg="yellow"))
            if os.path.exists("/Applications/Docker.app"):
                subprocess.run(["open", "-a", "Docker"], check=False)
                click.echo(click.style("   Please wait for Docker to fully start before proceeding.", fg="yellow"))
        
    if kube_running:
        click.echo(click.style(" ✓ Kubernetes is running.", fg="green"))
    else:
        click.echo(click.style(" ✗ Kubernetes is NOT reachable.", fg="red"))
        
    if not docker_running and not dry_run:
        click.echo(click.style("\n🚨 Docker must be running to start the control plane. Please wait for it to start and run `fleet bootstrap` again.", fg="red", bold=True))
        return
    elif not docker_running and dry_run:
        click.echo(click.style("\nℹ️ Docker is required. In a real run, fleet would wait for Docker here.", fg="cyan"))

    click.echo("\n[Phase 2: Bootstrapping Tier 0 Services]")
    
    def is_repo_dirty(cwd):
        try:
            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=cwd, text=True, stderr=subprocess.DEVNULL)
            return bool(status.strip())
        except Exception:
            return False

    def run_docker_service(cwd, name):
        expanded_cwd = os.path.expanduser(cwd)
        cmd = "docker compose up -d"
        if dry_run:
            click.echo(f" + [DRY RUN] Would start {name}")
            click.echo(f"   > cd {cwd} && {cmd}")
            return True

        click.echo(f" + Starting {name} in {cwd}...")
        if not os.path.exists(expanded_cwd):
            click.echo(click.style(f"   ✗ Directory not found: {expanded_cwd}", fg="red"))
            return False
        try:
            subprocess.run(cmd, cwd=expanded_cwd, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            click.echo(click.style(f"   ✓ {name} initiated.", fg="green"))
            return True
        except Exception as e:
            click.echo(click.style(f"   ✗ Failed to start {name}: {e}", fg="red"))
            return False

    def run_go_service(cwd, name):
        expanded_cwd = os.path.expanduser(cwd)
        binary_path = os.path.join(expanded_cwd, name)
        
        needs_build = not os.path.exists(binary_path)
        if force:
            needs_build = True

        repo_dirty = False
        if os.path.exists(expanded_cwd):
            repo_dirty = is_repo_dirty(expanded_cwd)

        if needs_build:
            if repo_dirty and not dirty:
                click.echo(click.style(f" + Cannot start {name} in {cwd}...", fg="red"))
                click.echo(click.style(f"   ✗ Repository is dirty and requires a build. Pass --dirty to build anyway.", fg="red"))
                return False
            cmd = f"go build ./cmd/{name} && ./{name} &"
            action_desc = "Would build and start" if dry_run else "Building and starting"
        else:
            cmd = f"./{name} &"
            action_desc = "Would run existing binary for" if dry_run else "Running existing binary for"
            if repo_dirty:
                click.echo(click.style(f"   ℹ️ {name} repo is dirty, but running existing binary.", fg="yellow"))

        if dry_run:
            click.echo(f" + [DRY RUN] {action_desc} {name}")
            click.echo(f"   > cd {cwd} && {cmd}")
            return True

        click.echo(f" + {action_desc} {name} in {cwd}...")
        if not os.path.exists(expanded_cwd):
            click.echo(click.style(f"   ✗ Directory not found: {expanded_cwd}", fg="red"))
            return False
        try:
            actual_cmd = cmd.replace("&", "").strip()
            subprocess.Popen(actual_cmd, cwd=expanded_cwd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            click.echo(click.style(f"   ✓ {name} initiated.", fg="green"))
            return True
        except Exception as e:
            click.echo(click.style(f"   ✗ Failed to start {name}: {e}", fg="red"))
            return False

    run_docker_service("~/work/traefik", "traefik")
    run_docker_service("~/work/delightd", "delightd")
    run_docker_service("~/work/transparent", "transparent")
    
    click.echo("\n" + "-"*60)
    click.echo(click.style("💡 To get the fleet running, you have the following options:", bold=True, fg="cyan"))
    click.echo(" • " + click.style("Default:", bold=True) + " Dirty repos run their existing binaries as-is (safest).")
    click.echo(" • " + click.style("--force:", bold=True) + " Blows away existing binaries (BUT NOT SOURCE CODE) and attempts to arouse the fleet.")
    click.echo(" • " + click.style("--dirty:", bold=True) + " Bypasses the safety check, allowing a rebuild from uncommitted dirty source code.")
    click.echo("-" * 60)
    
    if dry_run:
        click.echo(click.style("\n✅ Dry run complete. No actions were taken.", fg="green", bold=True))
        click.echo("Remove --dry-run to execute.")
    else:
        click.echo(click.style("\n✅ Bootstrap complete. The routing layer will reconstruct automatically.", fg="green", bold=True))
        click.echo("Run `fleet show` to check the status once services are online.")

@main.command()
def emergency_stop():
    """
    Emergency Lever: Forcefully evict all mesh resources from memory.
    Kills all Docker containers and background LLM servers.
    """
    click.echo(click.style("🚨 EMERGENCY LEVER PULLED: Evicting all resources...", fg="red", bold=True))
    
    import subprocess
    
    # 1. Kill all Docker Containers
    click.echo(" + Halting all Docker containers in the mesh...")
    try:
        ps_out = subprocess.check_output(["docker", "ps", "-q"]).decode().strip()
        if ps_out:
            containers = ps_out.split()
            subprocess.run(["docker", "stop"] + containers, check=False)
            click.echo(f"   Stopped {len(containers)} containers.")
    except Exception as e:
        click.echo(f"   Docker halt failed: {e}")

    # 2. Kill all background LLM servers (Ollama, llama-server)
    click.echo(" + Force killing LLM servers (llama-server, ollama)...")
    try:
        subprocess.run(["killall", "-9", "llama-server", "ollama"], stderr=subprocess.DEVNULL, check=False)
        click.echo("   LLM servers destroyed.")
    except Exception:
        pass

    click.echo(click.style("✅ HOST MEMORY PURGING INITIATED.", fg="green", bold=True))

from fleet.git_cli import git
main.add_command(git)

if __name__ == '__main__':
    main()

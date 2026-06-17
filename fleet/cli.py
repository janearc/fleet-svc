import os
import shutil
import subprocess
import sys

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

_COMPOSE_FILENAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

# Used only when WorkstationConfig cannot be read: a meteor may have taken the
# config with everything else, and cold-boot recovery must not hard-depend on it.
_FALLBACK_TIER0 = (
    ("traefik", "~/work/traefik"),
    ("delightd", "~/work/delightd"),
    ("transparent", "~/work/transparent"),
)


def _locate_workstation_config():
    import os
    for candidate in ("WorkstationConfig.yaml", os.path.expanduser("~/work/fleet/WorkstationConfig.yaml")):
        if os.path.exists(candidate):
            return candidate
    return "WorkstationConfig.yaml"


def _essential_compose_repos(config_path=None):
    # Tier-0 is every repository marked essential in WorkstationConfig that
    # actually ships a compose file. Deriving the set from the declarative config
    # (instead of a hardcoded list) is what lets kafka-logging -- the message
    # backbone -- come up on a cold boot. fleet itself is essential but has no
    # compose target, so the compose-file filter correctly excludes the
    # orchestrator that is running this very command.
    import os

    import yaml

    from fleet.models import WorkstationConfig

    config_path = config_path or _locate_workstation_config()
    with open(config_path) as handle:
        config = WorkstationConfig(**yaml.safe_load(handle))

    repos = []
    for repo in config.repositories:
        if not repo.essential:
            continue
        expanded = os.path.expanduser(repo.path)
        if any(os.path.exists(os.path.join(expanded, name)) for name in _COMPOSE_FILENAMES):
            repos.append((repo.name, repo.path))
    return repos


# Runtime override env var. Highest precedence -- lets a one-off invocation pin
# the engine without editing WorkstationConfig (CI, a borrowed host, debugging).
_DOCKER_RUNTIME_ENV = "FLEET_DOCKER_RUNTIME"

# What fleet knows how to drive. `is_available` is evaluated at call time so the
# env can be patched in tests; `start_cmd` only kicks the engine (we never block
# on VM boot -- the operator re-runs bootstrap once it reports ready).
_DOCKER_RUNTIMES = {
    "colima": {
        "label": "colima",
        "is_available": lambda: shutil.which("colima") is not None,
        "start_cmd": ["colima", "start"],
    },
    "docker-desktop": {
        "label": "Docker Desktop",
        "is_available": lambda: os.path.exists("/Applications/Docker.app"),
        "start_cmd": ["open", "-a", "Docker"],
    },
}

# Order in which "auto" sniffs. colima first: on a memory-constrained host Docker
# Desktop's idle footprint is the liability we are retiring.
_AUTO_PREFERENCE = ("colima", "docker-desktop")

# "auto" is a resolver directive, not a drivable runtime, so it is valid input
# but absent from _DOCKER_RUNTIMES.
_VALID_RUNTIMES = frozenset({"auto"}) | frozenset(_DOCKER_RUNTIMES)


def _resolve_docker_runtime(config_path=None):
    # Precedence: $FLEET_DOCKER_RUNTIME > WorkstationConfig host.docker_runtime >
    # "auto". The config read is best-effort -- a meteor may have taken the file
    # with everything else, and cold-boot recovery must still pick a runtime.
    # Returns one of _VALID_RUNTIMES; "auto" is expanded later by sniffing.
    env_choice = os.environ.get(_DOCKER_RUNTIME_ENV)
    if env_choice:
        env_choice = env_choice.strip().lower()
        if env_choice not in _VALID_RUNTIMES:
            raise ValueError(
                f"{_DOCKER_RUNTIME_ENV}={env_choice!r} is not a known runtime "
                f"(choose from {sorted(_VALID_RUNTIMES)})"
            )
        return env_choice

    import yaml

    from fleet.models import WorkstationConfig

    try:
        config_path = config_path or _locate_workstation_config()
        with open(config_path) as handle:
            config = WorkstationConfig(**yaml.safe_load(handle))
        return config.host.docker_runtime
    except Exception:
        return "auto"


def _start_docker_runtime(dry_run, runtime=None):
    # Resolve the runtime, then enforce the contract: an *explicit* choice that is
    # not installed is a hard stop -- we refuse to silently start a different
    # engine (the exact failure that left a Docker Desktop daemon shadowing the
    # colima fleet). Only "auto" is allowed to fall back, and only by sniffing.
    if runtime is None:
        try:
            runtime = _resolve_docker_runtime()
        except ValueError as exc:
            click.echo(click.style(f" ✗ {exc}", fg="red"))
            return

    if runtime == "auto":
        resolved = next(
            (candidate for candidate in _AUTO_PREFERENCE if _DOCKER_RUNTIMES[candidate]["is_available"]()),
            None,
        )
        if resolved is None:
            click.echo(click.style(" ✗ No Docker runtime found (neither colima nor Docker Desktop).", fg="red"))
            # The runtimes fleet can *start* are macOS-shaped (colima, Docker
            # Desktop.app). On a non-darwin host this is not a crash -- there is
            # simply nothing to launch; bring up the native daemon and re-run.
            if sys.platform != "darwin":
                click.echo(click.style(
                    "   On this host fleet cannot start the engine for you; start your docker "
                    "daemon directly (e.g. `systemctl start docker`) and re-run `fleet bootstrap`.",
                    fg="yellow"))
            return
    else:
        resolved = runtime
        if not _DOCKER_RUNTIMES[resolved]["is_available"]():
            label = _DOCKER_RUNTIMES[resolved]["label"]
            click.echo(click.style(
                f" ✗ Configured docker runtime '{resolved}' ({label}) is not installed on this host.",
                fg="red"))
            click.echo(click.style(
                f"   Refusing to start a different engine. Install {label}, or set host.docker_runtime "
                f"(or ${_DOCKER_RUNTIME_ENV}) to 'auto' to allow sniffing.", fg="red"))
            return

    spec = _DOCKER_RUNTIMES[resolved]
    label, cmd = spec["label"], spec["start_cmd"]

    if dry_run:
        click.echo(click.style(f" ✗ Docker is NOT running. Would start it via {label}: {' '.join(cmd)}", fg="yellow"))
        return

    click.echo(click.style(f" ✗ Docker is NOT running. Starting the engine via {label}...", fg="yellow"))
    subprocess.run(cmd, check=False)
    click.echo(click.style("   Wait for the engine to report ready, then re-run `fleet bootstrap`.", fg="yellow"))


@main.command()
@click.option('--dry-run', is_flag=True, help='Preview what will be started without taking action')
@coro
async def bootstrap(dry_run):
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
        _start_docker_runtime(dry_run)
        
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

    try:
        tier0 = _essential_compose_repos()
        if not tier0:
            raise ValueError("no essential compose repositories resolved from WorkstationConfig")
    except Exception as exc:
        click.echo(click.style(f" ! Could not derive tier-0 from WorkstationConfig ({exc}); using fallback list.", fg="yellow"))
        tier0 = list(_FALLBACK_TIER0)

    for name, path in tier0:
        run_docker_service(path, name)

    click.echo("\n" + "-" * 60)
    click.echo(click.style("💡 Tier-0 is derived from essential repositories in WorkstationConfig.", fg="cyan"))
    click.echo("   Edit that file to change what a cold boot brings up.")
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

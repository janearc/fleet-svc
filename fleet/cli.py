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

@main.command(help="ensure no repositories are dirty before teardown")
def sync():
    import sys

    from fleet.git_state import DelightdUnavailable, fetch_git_state

    try:
        git_repos = fetch_git_state()
    except DelightdUnavailable as exc:
        # fail closed: delightd is the source of truth; if it can't answer we
        # cannot certify the workstation safe to tear down
        click.echo(click.style(f"blocked: cannot verify git state, delightd unreachable ({exc})", fg="red"))
        click.echo("start delightd and retry before a host migration or teardown.")
        sys.exit(1)

    dirty = [r["name"] for r in git_repos if r.get("dirty")]
    unpushed = [r["name"] for r in git_repos if r.get("unpushed", 0) > 0]
    # a project whose path is gone on disk is delightd reporting a STALE roster
    # entry, not a risk to data: there is no working tree to lose. it is reported
    # by delightd as `missing_path: true` (alongside a git.error explaining why).
    # downgrade it to a warning and do NOT let it block teardown by itself --
    # everything ELSE that delightd could not verify still fails closed below.
    missing_path = [r["name"] for r in git_repos if r.get("missing_path")]
    # an unreadable repo is unsafe: the teardown gate fails closed, never
    # assuming "clean" for state it could not verify. a missing_path project also
    # carries git.error ("project path not found"), but that specific cause is
    # benign, so it is excluded from the failing-closed set.
    errored = [
        r["name"]
        for r in git_repos
        if r.get("error") and not r.get("missing_path")
    ]

    for name in missing_path:
        click.echo(click.style(
            f"warning: project {name} path missing on disk -- stale config entry? "
            "(not blocking teardown)",
            fg="yellow"))

    if dirty or unpushed or errored:
        click.echo(click.style("\nblocked: workstation has uncommitted, unpushed, or unverifiable state", fg="red"))
        if dirty:
            click.echo(f"dirty: {', '.join(dirty)}")
        if unpushed:
            click.echo(f"unpushed: {', '.join(unpushed)}")
        if errored:
            click.echo(f"could not verify (failing closed): {', '.join(errored)}")
        click.echo("\ncommit and push all changes before a host migration or teardown.")
        sys.exit(1)

    click.echo(click.style("workstation is clean and safe to teardown", fg="green"))

_COMPOSE_FILENAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

# Used only when WorkstationConfig cannot be read: a meteor may have taken the
# config with everything else, and cold-boot recovery must not hard-depend on it.
# Ordered network-owner-first to match the lifecycle model: traefik creates the
# dev-fleet network and is the route registry, so it must come up before any
# dependent. kafka-logging is the message backbone -- a cold boot that omits it
# leaves every producer/consumer with nowhere to talk, so it belongs in the
# minimal recoverable set (the prior fallback wrongly dropped it).
_FALLBACK_TIER0 = (
    ("traefik", "~/work/traefik"),
    ("kafka-logging", "~/work/kafka-logging"),
    ("delightd", "~/work/delightd"),
)


# raised by the bootstrap network-precedence guard when a dependent service would
# be started before the dev-fleet network exists. surfaced as one actionable line
# instead of letting the raw compose "network dev-fleet not found" leak out.
class NetworkNotReady(Exception):
    pass


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


def _dev_fleet_network_exists():
    # cheap, side-effect-free probe of the shared network. used by bootstrap to
    # refuse to start a dependent before its network owner has created dev-fleet,
    # turning the raw compose "network dev-fleet not found" into an actionable
    # error. any docker failure is treated as "cannot confirm" -> absent.
    from fleet.lifecycle import DEV_FLEET_NETWORK

    try:
        out = subprocess.check_output(
            ["docker", "network", "ls", "--filter", f"name=^{DEV_FLEET_NETWORK}$", "--format", "{{.Name}}"],
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return DEV_FLEET_NETWORK in out.decode().split()


def _resolve_lifecycle_plan(config_path=None):
    # single source of the ordered fleet membership: load the roster and classify
    # each compose-bearing repo into a tier. used by both bootstrap (forward) and
    # down (reverse) so ordering is defined once.
    from fleet.lifecycle import load_plan

    config_path = config_path or _locate_workstation_config()
    return load_plan(config_path)


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

    # forward (up) order comes from the one lifecycle model: network owner first,
    # then backbone, control plane, workloads. the same model drives `fleet down`
    # in reverse, so ordering lives in exactly one place.
    network_owner = None
    try:
        plan = _resolve_lifecycle_plan()
        ordered = [(u.name, u.path) for u in plan.up_order]
        network_owner = plan.network_owner
        if not ordered:
            raise ValueError("no essential compose repositories resolved from WorkstationConfig")
    except Exception as exc:
        click.echo(click.style(f" ! Could not derive tier-0 from WorkstationConfig ({exc}); using fallback list.", fg="yellow"))
        ordered = list(_FALLBACK_TIER0)

    # network precedence: bring up the owner first (it creates dev-fleet), then
    # confirm the network exists before starting any dependent. if it is still
    # absent we stop with one actionable line rather than letting every dependent
    # fail with the raw compose "network dev-fleet not found".
    owner_name = network_owner.name if network_owner is not None else (ordered[0][0] if ordered else None)

    for name, path in ordered:
        is_owner = (name == owner_name)
        if not is_owner and not dry_run and not _dev_fleet_network_exists():
            from fleet.lifecycle import DEV_FLEET_NETWORK
            click.echo(click.style(
                f"\n🚨 The shared '{DEV_FLEET_NETWORK}' network does not exist yet; cannot start {name}.",
                fg="red", bold=True))
            click.echo(click.style(
                f"   The network owner ({owner_name}) must come up first to create it. "
                f"Wait for {owner_name} to be ready, then re-run `fleet bootstrap`.",
                fg="red"))
            return
        run_docker_service(path, name, dry_run)

    click.echo("\n" + "-" * 60)
    click.echo(click.style("💡 Start order is derived from the lifecycle model (network owner first).", fg="cyan"))
    click.echo("   Membership comes from essential repositories in WorkstationConfig.")
    click.echo("   Edit that file to change what a cold boot brings up.")
    click.echo("-" * 60)

    if dry_run:
        click.echo(click.style("\n✅ Dry run complete. No actions were taken.", fg="green", bold=True))
        click.echo("Remove --dry-run to execute.")
    else:
        click.echo(click.style("\n✅ Bootstrap complete. The routing layer will reconstruct automatically.", fg="green", bold=True))
        click.echo("Run `fleet show` to check the status once services are online.")

def run_docker_service(cwd, name, dry_run):
    # graceful, scoped START of one fleet repo's compose project via
    # `docker compose up -d`. like the stop counterpart it only acts on the
    # compose project rooted in `cwd`.
    import os

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


def _compose_stop_repo(name, path, dry_run):
    # graceful, scoped stop of ONE fleet repo's compose project. `docker compose
    # stop` acts only on the containers of the compose project rooted at `cwd`,
    # so this never reaches the k3d cluster containers (k3d-fleet-*) or any
    # container outside the fleet's own compose files -- the scoping is the
    # compose project, not a flat `docker stop` of `docker ps -q`. idempotent:
    # stopping an already-stopped project is a no-op that exits 0.
    import os

    expanded = os.path.expanduser(path)
    cmd = "docker compose stop"
    if dry_run:
        click.echo(f" + [DRY RUN] Would stop {name}")
        click.echo(f"   > cd {path} && {cmd}")
        return True

    click.echo(f" + Stopping {name} in {path}...")
    if not os.path.exists(expanded):
        # a missing repo dir is not an error for teardown: nothing to stop.
        click.echo(click.style(f"   ⚠ Directory not found, nothing to stop: {expanded}", fg="yellow"))
        return True
    try:
        subprocess.run(cmd, cwd=expanded, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        click.echo(click.style(f"   ✓ {name} stopped.", fg="green"))
        return True
    except Exception as e:
        click.echo(click.style(f"   ✗ Failed to stop {name}: {e}", fg="red"))
        return False


@main.command(help="graceful, dependency-ordered teardown of the fleet's own compose services")
@click.option('--dry-run', is_flag=True, help='Print the teardown plan without acting')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
@click.option('--skip-sync', is_flag=True, help='Do not gate on a clean git tree (sync) first')
def down(dry_run, yes, skip_sync):
    # graceful, fleet-SCOPED teardown in REVERSE dependency order: workloads
    # (obs-svc, paling) first, then control plane (delightd), then the message
    # backbone, then the network owner (traefik) LAST -- it owns dev-fleet and
    # the route registry, so nothing else can be talking by the time it goes.
    #
    # scoping: the set comes from the lifecycle plan (WorkstationConfig roster +
    # compose files), and each repo is stopped with `docker compose stop` against
    # its own project. it NEVER touches the k3d cluster containers or colima --
    # those are not roster repos and `docker compose` cannot reach across projects.
    #
    # state model: `down` does NOT journal. the declared essential set in
    # WorkstationConfig is the single source of truth; `fleet bootstrap`
    # reconverges to it. (the pause/resume journal is a separate, selective lever
    # and is left intact.) this is the simpler correct round-trip: down -> bootstrap
    # returns to the declared set without replaying recorded per-service state.
    title = "🛑 Graceful Fleet Teardown"
    if dry_run:
        title += " (DRY RUN)"
    click.echo(click.style(title, bold=True, fg="cyan"))

    # gate on a clean tree unless explicitly skipped or dry-running. we do not
    # silently tear down a dirty workstation: uncommitted/unpushed state would be
    # at risk. the operator can run `fleet sync` themselves, or pass --skip-sync.
    if not dry_run and not skip_sync:
        from fleet.git_state import DelightdUnavailable, fetch_git_state
        try:
            git_repos = fetch_git_state()
            dirty = [r["name"] for r in git_repos if r.get("dirty") or r.get("unpushed", 0) > 0 or r.get("error")]
            if dirty:
                click.echo(click.style(
                    f"\n🚨 Refusing to tear down: workstation has uncommitted/unpushed/unverifiable state ({', '.join(dirty)}).",
                    fg="red", bold=True))
                click.echo("   Run `fleet sync` and resolve it, or pass --skip-sync to override.")
                sys.exit(1)
        except DelightdUnavailable as exc:
            click.echo(click.style(
                f"\n🚨 Refusing to tear down: cannot verify git state, delightd unreachable ({exc}).",
                fg="red", bold=True))
            click.echo("   Start delightd and retry, or pass --skip-sync to override.")
            sys.exit(1)

    try:
        plan = _resolve_lifecycle_plan()
        ordered = plan.down_order
        if not ordered:
            raise ValueError("no compose repositories resolved from WorkstationConfig")
    except Exception as exc:
        click.echo(click.style(f" ✗ Could not derive the teardown plan from WorkstationConfig: {exc}", fg="red"))
        click.echo("   Refusing a flat `docker stop` -- that is what emergency-stop is for.")
        sys.exit(1)

    click.echo("\n[Teardown order: workloads → control plane → backbone → network owner]")
    for unit in ordered:
        click.echo(f"   {unit.tier_label}: {unit.name}")

    if not dry_run and not yes:
        click.confirm("\nProceed with graceful teardown?", abort=True)

    click.echo("")
    # a service that will not stop is NOT something to paper over: continue the
    # teardown so the rest of the fleet still comes down, but record the failures
    # and surface them as a hard, non-zero error at the end naming each service.
    # a silent "stopped gracefully" over a container that is still running is the
    # exact lie this guard exists to prevent.
    failed: list[str] = []
    for unit in ordered:
        if not _compose_stop_repo(unit.name, unit.path, dry_run):
            failed.append(unit.name)

    click.echo("\n" + "-" * 60)
    click.echo(click.style("k3d cluster containers and colima are deliberately untouched.", fg="cyan"))
    click.echo("   `fleet bootstrap` reconverges to the declared essential set.")
    click.echo("-" * 60)

    if failed:
        click.echo(click.style(
            f"\nTeardown incomplete: could not stop {', '.join(failed)}. "
            "These services are still running; the fleet is not fully down.",
            fg="red", bold=True))
        click.echo(click.style(
            "   Investigate the failed compose project(s) and re-run `fleet down`, "
            "or pull `fleet emergency-stop` as a last resort.",
            fg="red"))
        sys.exit(1)

    if dry_run:
        click.echo(click.style("\nDry run complete. No actions were taken.", fg="green", bold=True))
    else:
        click.echo(click.style("\nFleet stopped gracefully. Run `fleet bootstrap` to bring it back.", fg="green", bold=True))


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

@main.command(
    name='model-svc',
    help="dispatch to the model-svc project's wrapper: fleet model-svc <deployment> <command> [args]",
    context_settings={"ignore_unknown_options": True},
)
@click.argument('deployment')
@click.argument('command')
@click.argument('args', nargs=-1, type=click.UNPROCESSED)
def model_svc(deployment, command, args):
    # forward to model-svc's bash wrapper (the contract). model-svc is a sibling
    # project subordinate to fleet; fleet does not reimplement it. propagate the
    # wrapper's exit code; if it is not installed, fail with one clear message.
    from fleet.model_svc import ModelSvcNotInstalled, dispatch

    try:
        code = dispatch(deployment, command, list(args))
    except ModelSvcNotInstalled as exc:
        click.echo(click.style(str(exc), fg="red"))
        sys.exit(1)
    sys.exit(code)


@main.command(name='pr-report', help="survey open PRs across the roster and classify each")
@click.option('--table', 'as_table', is_flag=True, help='Render a human table instead of JSON')
@click.option('--config', 'config_path', default=None, help='Path to WorkstationConfig.yaml')
def pr_report(as_table, config_path):
    # json by default per the agent-first mandate; --table is the human view.
    from fleet.pr_report import build_report
    from fleet.display import render_pr_report

    report = build_report(config_path=config_path)
    if as_table:
        render_pr_report(report)
        return
    click.echo(report.model_dump_json(indent=2))


@main.command(help="roll one fleet project, delightd-gated (udeploy-style): fleet deploy <project>")
@click.argument('project')
@click.option('--dry-run', is_flag=True, help='Show the roll plan without acting')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON (agent-first)')
@click.option('--config', 'config_path', default=None, help='Path to WorkstationConfig.yaml')
def deploy(project, dry_run, as_json, config_path):
    # fleet does not roll blind: deploy() asks delightd (the SOT) whether the
    # project is known + healthy and fails closed if it cannot answer, then rolls
    # it the way WorkstationConfig declares (kube rollout / launchd reload).
    from fleet.deploy import DeployError
    from fleet.deploy import deploy as run_deploy

    try:
        result = run_deploy(project, config_path=config_path, dry_run=dry_run)
    except DeployError as exc:
        click.echo(click.style(f"deploy refused: {exc}", fg="red"))
        sys.exit(1)

    if as_json:
        click.echo(result.model_dump_json(indent=2))
        return
    for warning in result.warnings:
        click.echo(click.style(f"  ⚠ {warning}", fg="yellow"))
    verb = "would roll" if result.dry_run else "rolled"
    click.echo(click.style(f"✓ {verb} {result.project} via {result.kind}: {result.detail}", fg="green"))


from fleet.git_cli import git
main.add_command(git)

if __name__ == '__main__':
    main()

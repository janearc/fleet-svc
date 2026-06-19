import pytest
from unittest.mock import patch, MagicMock, AsyncMock, mock_open
from click.testing import CliRunner
from fleet.cli import (
    main,
    _essential_compose_repos,
    _resolve_docker_runtime,
    _start_docker_runtime,
    _DOCKER_RUNTIME_ENV,
)
from fleet.models import FleetState, PauseResult, SourceHealth

@pytest.fixture
def cli_runner():
    return CliRunner()

@patch("fleet.cli.FleetCore")
def test_show_command(mock_core_cls, cli_runner, sample_fleet_state):
    mock_core = mock_core_cls.return_value
    mock_core.show = AsyncMock(return_value=sample_fleet_state)
    
    result = cli_runner.invoke(main, ["show", "--json"])
    assert result.exit_code == 0, f"Failed with output: {result.output}"
    assert "app-web" in result.output
    assert "app-worker" in result.output

@patch("fleet.cli.FleetCore")
def test_pause_dry_run(mock_core_cls, cli_runner, sample_services):
    mock_core = mock_core_cls.return_value
    mock_core.pause = AsyncMock(return_value=PauseResult(
        action="pause",
        dry_run=True,
        affected=[sample_services[1]],
        skipped=[sample_services[0]],
        errors=[]
    ))
    
    result = cli_runner.invoke(main, ["pause", "--dry-run"])
    assert result.exit_code == 0, f"Failed with output: {result.output}"
    assert "DRY RUN" in result.output
    assert "app-web" in result.output

@patch("fleet.cli.FleetCore")
def test_selfcheck_command(mock_core_cls, cli_runner, sample_source_health):
    mock_core = mock_core_cls.return_value
    mock_core.selfcheck = AsyncMock(return_value=sample_source_health)
    
    result = cli_runner.invoke(main, ["selfcheck"])
    assert result.exit_code == 0, f"Failed with output: {result.output}"
    assert "docker" in result.output
    assert "kube" in result.output

@patch("fleet.cli.FleetCore")
def test_pause_yes(mock_core_cls, cli_runner, sample_services):
    mock_core = mock_core_cls.return_value
    mock_core.pause = AsyncMock(return_value=PauseResult(
        action="pause", dry_run=False, affected=[sample_services[1]], skipped=[], errors=[]
    ))
    result = cli_runner.invoke(main, ["pause", "--yes"])
    assert result.exit_code == 0

@patch("fleet.cli.FleetCore")
def test_resume_yes(mock_core_cls, cli_runner, sample_services):
    mock_core = mock_core_cls.return_value
    mock_core.resume = AsyncMock(return_value=PauseResult(
        action="resume", dry_run=False, affected=[sample_services[1]], skipped=[], errors=[]
    ))
    result = cli_runner.invoke(main, ["resume", "--yes"])
    assert result.exit_code == 0

@patch("uvicorn.run")
def test_serve(mock_run, cli_runner):
    result = cli_runner.invoke(main, ["serve", "--port", "1234"])
    assert result.exit_code == 0
    mock_run.assert_called_once()

@patch("fleet.cli.FleetCore")
def test_models_ls(mock_core_cls, cli_runner):
    mock_core = mock_core_cls.return_value
    mock_core.models = AsyncMock(return_value=[
        {"provider": "ollama", "url": "http://localhost:11434", "models": ["llama3", "mistral"], "healthy": True}
    ])
    result = cli_runner.invoke(main, ["models", "ls"])
    assert result.exit_code == 0
    assert "ollama" in result.output
    assert "llama3" in result.output

@patch("fleet.cli.FleetCore")
def test_models_ls_empty(mock_core_cls, cli_runner):
    mock_core = mock_core_cls.return_value
    mock_core.models = AsyncMock(return_value=[])
    result = cli_runner.invoke(main, ["models", "ls"])
    assert result.exit_code == 0
    assert "No local LLM sources discovered." in result.output

def test_apply_success(cli_runner):
    # Use mock_open: its read() returns the data once and then "" (EOF). A
    # hand-rolled MagicMock whose read() returns the same chunk forever sends
    # yaml.safe_load's Reader into an unbounded read loop -- CPU spin plus
    # unbounded buffer growth. That is the OOM hazard tracked in the
    # apply-config-oom issue, so this test must not reintroduce the pattern.
    config_yaml = """
version: "1.0"
host:
  os: "darwin"
  arch: "arm64"
  daemons: ["docker"]
repositories:
  - name: "fleet"
    origin: "git"
    path: "/tmp/fleet"
    essential: true
models:
  - provider: "ollama"
    id: "llama3.1:latest"
"""
    with patch("builtins.open", mock_open(read_data=config_yaml)):
        result = cli_runner.invoke(main, ["apply", "dummy.yaml"])
    assert result.exit_code == 0, f"Failed with output: {result.output}"
    assert "Applying WorkstationConfig v1.0" in result.output
    assert "Verify daemon: docker" in result.output
    assert "git clone git /tmp/fleet" in result.output

@patch("fleet.git_state.fetch_git_state")
def test_sync_clean(mock_fetch, cli_runner):
    mock_fetch.return_value = [
        {"name": "test-repo", "dirty": False, "unpushed": 0, "has_upstream": True, "error": ""},
    ]
    result = cli_runner.invoke(main, ["sync"])
    assert result.exit_code == 0
    assert "workstation is clean and safe to teardown" in result.output

@patch("fleet.git_state.fetch_git_state")
def test_sync_dirty(mock_fetch, cli_runner):
    mock_fetch.return_value = [
        {"name": "dirty-repo", "dirty": True, "unpushed": 2, "has_upstream": True, "error": ""},
    ]
    result = cli_runner.invoke(main, ["sync"])
    assert result.exit_code == 1
    assert "blocked" in result.output
    assert "dirty-repo" in result.output

@patch("fleet.git_state.fetch_git_state")
def test_sync_unreadable_fails_closed(mock_fetch, cli_runner):
    # a project whose state could not be verified must block teardown, not pass
    mock_fetch.return_value = [
        {"name": "mystery-repo", "dirty": False, "unpushed": 0, "has_upstream": False, "error": "not a git checkout"},
    ]
    result = cli_runner.invoke(main, ["sync"])
    assert result.exit_code == 1
    assert "blocked" in result.output
    assert "mystery-repo" in result.output

@patch("fleet.git_state.fetch_git_state")
def test_sync_fails_closed_when_delightd_down(mock_fetch, cli_runner):
    # no fleet-side fallback: if delightd can't answer, sync blocks rather than
    # certifying the workstation clean on unverified state
    from fleet.git_state import DelightdUnavailable
    mock_fetch.side_effect = DelightdUnavailable("connection refused")
    result = cli_runner.invoke(main, ["sync"])
    assert result.exit_code == 1
    assert "delightd unreachable" in result.output


def test_essential_compose_repos_filters(tmp_path):
    # essential + ships a compose file -> tier-0
    (tmp_path / "kafka").mkdir()
    (tmp_path / "kafka" / "docker-compose.yml").write_text("services: {}\n")
    # essential but no compose (the orchestrator) -> excluded
    (tmp_path / "fleet").mkdir()
    cfg = tmp_path / "WorkstationConfig.yaml"
    cfg.write_text(
        'version: "1.0"\n'
        "host: {os: darwin, arch: arm64, daemons: [docker]}\n"
        "repositories:\n"
        f'  - {{name: kafka-logging, origin: git, path: "{tmp_path}/kafka", essential: true}}\n'
        f'  - {{name: fleet, origin: git, path: "{tmp_path}/fleet", essential: true}}\n'
        f'  - {{name: comfyui, origin: git, path: "{tmp_path}/kafka", essential: false}}\n'
    )
    repos = _essential_compose_repos(str(cfg))
    # only the essential repo that has a compose file survives
    assert [name for name, _ in repos] == ["kafka-logging"]


@patch("fleet.cli._resolve_lifecycle_plan")
@patch("fleet.cli.FleetCore")
def test_bootstrap_starts_config_derived_tier0(mock_core_cls, mock_plan, cli_runner):
    # the message backbone must appear in a cold boot, not just the legacy trio
    from fleet.lifecycle import LifecyclePlan, LifecycleUnit, TIER_NETWORK_OWNER, TIER_BACKBONE
    mock_plan.return_value = LifecyclePlan(units=[
        LifecycleUnit(name="traefik", path="~/work/traefik", tier=TIER_NETWORK_OWNER, essential=True),
        LifecycleUnit(name="kafka-logging", path="~/work/kafka-logging", tier=TIER_BACKBONE, essential=True),
    ])
    mock_core = mock_core_cls.return_value
    mock_core.selfcheck = AsyncMock(return_value=[
        SourceHealth(name="docker", reachable=True),
        SourceHealth(name="kube", reachable=False),
    ])
    result = cli_runner.invoke(main, ["bootstrap", "--dry-run"])
    assert result.exit_code == 0, f"Failed with output: {result.output}"
    assert "kafka-logging" in result.output
    assert "traefik" in result.output


@patch("fleet.cli._resolve_lifecycle_plan", side_effect=ValueError("config gone"))
@patch("shutil.which", return_value="/opt/homebrew/bin/colima")
@patch("fleet.cli.FleetCore")
def test_bootstrap_prefers_colima_when_docker_down(mock_core_cls, mock_which, mock_plan, cli_runner):
    mock_core = mock_core_cls.return_value
    mock_core.selfcheck = AsyncMock(return_value=[SourceHealth(name="docker", reachable=False)])
    result = cli_runner.invoke(main, ["bootstrap", "--dry-run"])
    assert result.exit_code == 0, f"Failed with output: {result.output}"
    assert "colima" in result.output.lower()
    # an unreadable plan must fall back rather than silently starting nothing
    assert "fallback" in result.output.lower()


# --- docker runtime selection --------------------------------------------

def _write_config(tmp_path, docker_runtime=None):
    runtime_line = f'  docker_runtime: "{docker_runtime}"\n' if docker_runtime else ""
    cfg = tmp_path / "WorkstationConfig.yaml"
    cfg.write_text(
        'version: "1.0"\n'
        "host:\n"
        "  os: darwin\n"
        "  arch: arm64\n"
        f"{runtime_line}"
        "  daemons: [docker]\n"
        "repositories:\n"
        f'  - {{name: fleet, origin: git, path: "{tmp_path}/fleet", essential: true}}\n'
    )
    return str(cfg)


def test_resolve_runtime_env_overrides_config(tmp_path, monkeypatch):
    # env wins even when the config pins something else
    monkeypatch.setenv(_DOCKER_RUNTIME_ENV, "docker-desktop")
    cfg = _write_config(tmp_path, docker_runtime="colima")
    assert _resolve_docker_runtime(cfg) == "docker-desktop"


def test_resolve_runtime_env_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(_DOCKER_RUNTIME_ENV, "  Colima  ")
    assert _resolve_docker_runtime("does-not-exist.yaml") == "colima"


def test_resolve_runtime_env_invalid_raises(monkeypatch):
    monkeypatch.setenv(_DOCKER_RUNTIME_ENV, "podman")
    with pytest.raises(ValueError, match="not a known runtime"):
        _resolve_docker_runtime("does-not-exist.yaml")


def test_resolve_runtime_reads_config(tmp_path, monkeypatch):
    monkeypatch.delenv(_DOCKER_RUNTIME_ENV, raising=False)
    cfg = _write_config(tmp_path, docker_runtime="docker-desktop")
    assert _resolve_docker_runtime(cfg) == "docker-desktop"


def test_resolve_runtime_defaults_auto_when_field_absent(tmp_path, monkeypatch):
    monkeypatch.delenv(_DOCKER_RUNTIME_ENV, raising=False)
    cfg = _write_config(tmp_path, docker_runtime=None)
    assert _resolve_docker_runtime(cfg) == "auto"


def test_resolve_runtime_defaults_auto_when_config_missing(monkeypatch):
    # a meteor took the config; cold boot must still resolve a runtime
    monkeypatch.delenv(_DOCKER_RUNTIME_ENV, raising=False)
    assert _resolve_docker_runtime("/nonexistent/WorkstationConfig.yaml") == "auto"


@patch("fleet.cli.subprocess.run")
@patch("os.path.exists", return_value=False)
def test_start_runtime_explicit_unavailable_refuses(mock_exists, mock_run, capsys):
    # docker-desktop is pinned but not installed -> hard stop, never start colima
    _start_docker_runtime(dry_run=False, runtime="docker-desktop")
    out = capsys.readouterr().out
    assert "not installed" in out
    assert "Refusing" in out
    mock_run.assert_not_called()


@patch("fleet.cli.subprocess.run")
@patch("shutil.which", return_value="/opt/homebrew/bin/colima")
def test_start_runtime_explicit_starts_when_available(mock_which, mock_run, capsys):
    _start_docker_runtime(dry_run=False, runtime="colima")
    out = capsys.readouterr().out
    assert "colima" in out.lower()
    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["colima", "start"]


@patch("fleet.cli.subprocess.run")
@patch("shutil.which", return_value="/opt/homebrew/bin/colima")
def test_start_runtime_auto_prefers_colima(mock_which, mock_run, capsys):
    _start_docker_runtime(dry_run=False, runtime="auto")
    out = capsys.readouterr().out
    assert "colima" in out.lower()
    assert mock_run.call_args.args[0] == ["colima", "start"]


@patch("fleet.cli.subprocess.run")
@patch("os.path.exists", return_value=True)
@patch("shutil.which", return_value=None)
def test_start_runtime_auto_falls_back_to_desktop(mock_which, mock_exists, mock_run, capsys):
    # colima absent, Docker Desktop present -> auto picks Desktop
    _start_docker_runtime(dry_run=False, runtime="auto")
    out = capsys.readouterr().out
    assert "Docker Desktop" in out
    assert mock_run.call_args.args[0] == ["open", "-a", "Docker"]


@patch("fleet.cli.subprocess.run")
@patch("fleet.cli.sys.platform", "linux")
@patch("os.path.exists", return_value=False)
@patch("shutil.which", return_value=None)
def test_start_runtime_non_darwin_hints_native_daemon(mock_which, mock_exists, mock_run, capsys):
    # on a non-macOS host there is nothing to start (the runtimes are mac-shaped),
    # but it must degrade with a useful hint, not crash and not run `open`
    _start_docker_runtime(dry_run=False, runtime="auto")
    out = capsys.readouterr().out
    assert "No Docker runtime found" in out
    assert "systemctl start docker" in out
    mock_run.assert_not_called()


@patch("fleet.cli.subprocess.run")
def test_start_runtime_invalid_env_reports_and_skips(mock_run, capsys, monkeypatch):
    # bad env propagates as a reported error, not a crash, and starts nothing
    monkeypatch.setenv(_DOCKER_RUNTIME_ENV, "podman")
    _start_docker_runtime(dry_run=False)
    out = capsys.readouterr().out
    assert "not a known runtime" in out
    mock_run.assert_not_called()


@patch("fleet.model_svc.dispatch", return_value=0)
def test_model_svc_dispatch_success(mock_dispatch, cli_runner):
    result = cli_runner.invoke(main, ["model-svc", "paling-dev", "status"])
    assert result.exit_code == 0
    mock_dispatch.assert_called_once_with("paling-dev", "status", [])


@patch("fleet.model_svc.dispatch", return_value=7)
def test_model_svc_propagates_exit_code(mock_dispatch, cli_runner):
    result = cli_runner.invoke(main, ["model-svc", "dev", "restart", "--force"])
    assert result.exit_code == 7
    mock_dispatch.assert_called_once_with("dev", "restart", ["--force"])


@patch("fleet.model_svc.dispatch")
def test_model_svc_not_installed_reports(mock_dispatch, cli_runner):
    from fleet.model_svc import ModelSvcNotInstalled
    mock_dispatch.side_effect = ModelSvcNotInstalled("model-svc not installed: ...")
    result = cli_runner.invoke(main, ["model-svc", "dev", "status"])
    assert result.exit_code == 1
    assert "model-svc not installed" in result.output


@patch("fleet.pr_report.build_report")
def test_pr_report_json_default(mock_build, cli_runner):
    # json by default: the agent-first contract
    from fleet.pr_report import PRReport, RepoReport
    mock_build.return_value = PRReport(
        repos=[RepoReport(name="fleet", path="~/work/fleet", slug="janearc/fleet-svc")]
    )
    result = cli_runner.invoke(main, ["pr-report"])
    assert result.exit_code == 0
    assert '"slug": "janearc/fleet-svc"' in result.output


@patch("fleet.display.render_pr_report")
@patch("fleet.pr_report.build_report")
def test_pr_report_table(mock_build, mock_render, cli_runner):
    from fleet.pr_report import PRReport
    mock_build.return_value = PRReport()
    result = cli_runner.invoke(main, ["pr-report", "--table"])
    assert result.exit_code == 0
    mock_render.assert_called_once()


# --- lifecycle: emergency-stop, down, bootstrap ordering -----------------

from fleet.cli import (  # noqa: E402
    _FALLBACK_TIER0,
    _locate_workstation_config,
    _resolve_lifecycle_plan,
)
from fleet.lifecycle import (  # noqa: E402
    LifecyclePlan,
    LifecycleUnit,
    TIER_BACKBONE,
    TIER_CONTROL_PLANE,
    TIER_NETWORK_OWNER,
    TIER_WORKLOAD,
)


def _full_plan():
    # network owner first, two workloads last -- the shape `down` reverses.
    return LifecyclePlan(units=[
        LifecycleUnit(name="traefik", path="~/work/traefik", tier=TIER_NETWORK_OWNER, essential=True),
        LifecycleUnit(name="kafka-logging", path="~/work/kafka-logging", tier=TIER_BACKBONE, essential=True),
        LifecycleUnit(name="delightd", path="~/work/delightd", tier=TIER_CONTROL_PLANE, essential=True),
        LifecycleUnit(name="obs-svc", path="~/work/obs-svc", tier=TIER_WORKLOAD, essential=False),
        LifecycleUnit(name="paling", path="~/work/paling", tier=TIER_WORKLOAD, essential=False),
    ])


def test_fallback_tier0_includes_kafka():
    # the cold-boot fallback (used when WorkstationConfig is unreadable) must
    # carry the message backbone; the prior list wrongly omitted it.
    names = [name for name, _ in _FALLBACK_TIER0]
    assert "kafka-logging" in names
    assert "traefik" in names
    # network owner must still be first in the fallback ordering
    assert names[0] == "traefik"


@patch("fleet.cli.subprocess.run")
@patch("fleet.cli.subprocess.check_output")
def test_emergency_stop_stops_all_containers(mock_check, mock_run, cli_runner):
    # codify the EXISTING behavior: emergency-stop is a flat `docker stop` of
    # every running container plus killall of the llm servers. (this is the blunt
    # lever; `fleet down` is the graceful, scoped one.)
    mock_check.return_value = b"abc123\ndef456\n"
    result = cli_runner.invoke(main, ["emergency-stop"])
    assert result.exit_code == 0
    # docker stop was called with ALL container ids from `docker ps -q`
    stop_calls = [c for c in mock_run.call_args_list if c.args and c.args[0][:2] == ["docker", "stop"]]
    assert stop_calls
    assert stop_calls[0].args[0] == ["docker", "stop", "abc123", "def456"]
    # the llm servers are force-killed
    kill_calls = [c for c in mock_run.call_args_list if c.args and c.args[0][0] == "killall"]
    assert kill_calls


@patch("fleet.cli._resolve_lifecycle_plan")
def test_down_reverse_dependency_order(mock_plan, cli_runner):
    # dry-run prints the plan; teardown order is reverse of up: workloads first,
    # network owner (traefik) last.
    mock_plan.return_value = _full_plan()
    result = cli_runner.invoke(main, ["down", "--dry-run"])
    assert result.exit_code == 0, f"Failed with output: {result.output}"
    out = result.output
    # workloads appear before the network owner in the stop sequence
    assert out.index("paling") < out.index("traefik")
    assert out.index("obs-svc") < out.index("delightd")
    assert out.index("delightd") < out.index("kafka-logging")
    assert out.index("kafka-logging") < out.index("traefik")


@patch("fleet.cli.subprocess.run")
@patch("fleet.cli._resolve_lifecycle_plan")
def test_down_excludes_k3d_and_colima(mock_plan, mock_run, cli_runner):
    # `fleet down` must NEVER reach the k3d cluster containers or colima. it acts
    # only via `docker compose stop` rooted in each roster repo, so no command it
    # issues names a k3d container or colima.
    mock_plan.return_value = _full_plan()
    result = cli_runner.invoke(main, ["down", "--yes", "--skip-sync"])
    assert result.exit_code == 0, f"Failed with output: {result.output}"
    # inspect the COMMANDS issued (not the explanatory footer, which mentions
    # k3d/colima precisely to say they are untouched). no command names a k3d
    # container or colima, and every action is a scoped `docker compose stop`.
    issued = [c.args[0] for c in mock_run.call_args_list if c.args]
    flat = " ".join(str(c) for c in issued)
    assert "k3d" not in flat
    assert "colima" not in flat
    for cmd in issued:
        assert cmd == "docker compose stop"
        assert "docker stop" not in cmd
    # and the cwd of every stop is a roster repo path, never a cluster
    cwds = [c.kwargs.get("cwd", "") for c in mock_run.call_args_list]
    assert all("k3d" not in cwd for cwd in cwds)


@patch("fleet.cli.subprocess.run")
@patch("fleet.cli._resolve_lifecycle_plan")
def test_down_gates_on_dirty_tree(mock_plan, mock_run, cli_runner):
    # a dirty workstation must block teardown unless --skip-sync is passed.
    mock_plan.return_value = _full_plan()
    with patch("fleet.git_state.fetch_git_state", return_value=[
        {"name": "delightd", "dirty": True, "unpushed": 0, "error": ""},
    ]):
        result = cli_runner.invoke(main, ["down", "--yes"])
    assert result.exit_code == 1
    assert "Refusing to tear down" in result.output
    mock_run.assert_not_called()


@patch("fleet.cli._dev_fleet_network_exists", return_value=False)
@patch("fleet.cli.run_docker_service")
@patch("fleet.cli._resolve_lifecycle_plan")
@patch("fleet.cli.FleetCore")
def test_bootstrap_starts_network_owner_before_dependents(
    mock_core_cls, mock_plan, mock_run_svc, mock_net, cli_runner
):
    # with docker up but the network absent, bootstrap starts the owner first,
    # then refuses to start a dependent with one actionable error instead of the
    # raw compose "network dev-fleet not found".
    mock_plan.return_value = _full_plan()
    mock_core = mock_core_cls.return_value
    mock_core.selfcheck = AsyncMock(return_value=[SourceHealth(name="docker", reachable=True)])
    result = cli_runner.invoke(main, ["bootstrap"])
    assert result.exit_code == 0, f"Failed with output: {result.output}"
    # the owner (traefik) was started; the network never appeared, so the first
    # dependent triggered the guard.
    started = [c.args[1] for c in mock_run_svc.call_args_list]
    assert started == ["traefik"]
    assert "dev-fleet" in result.output
    assert "network owner (traefik) must come up first" in result.output


@patch("fleet.cli._dev_fleet_network_exists", return_value=True)
@patch("fleet.cli.run_docker_service")
@patch("fleet.cli._compose_stop_repo")
@patch("fleet.cli._resolve_lifecycle_plan")
@patch("fleet.cli.FleetCore")
def test_down_up_roundtrip(
    mock_core_cls, mock_plan, mock_stop, mock_start, mock_net, cli_runner
):
    # down then bootstrap must converge back to the declared set, in mirror order.
    # both consume the SAME lifecycle plan; down stops in reverse, bootstrap starts
    # forward. no journal replay -- WorkstationConfig is the source of truth.
    plan = _full_plan()
    mock_plan.return_value = plan
    declared = {u.name for u in plan.units}

    # teardown (skip the sync gate; this is a mocked round-trip)
    down_result = cli_runner.invoke(main, ["down", "--yes", "--skip-sync"])
    assert down_result.exit_code == 0, f"down failed: {down_result.output}"
    stopped = [c.args[0] for c in mock_stop.call_args_list]
    assert stopped[0] == "paling" or plan.down_order[0].name == stopped[0]
    assert stopped[-1] == "traefik"  # network owner stopped last
    assert set(stopped) == declared

    # bootstrap back up
    mock_core = mock_core_cls.return_value
    mock_core.selfcheck = AsyncMock(return_value=[SourceHealth(name="docker", reachable=True)])
    up_result = cli_runner.invoke(main, ["bootstrap"])
    assert up_result.exit_code == 0, f"bootstrap failed: {up_result.output}"
    started = [c.args[1] for c in mock_start.call_args_list]
    assert started[0] == "traefik"  # network owner started first
    assert set(started) == declared

    # round-trip is a mirror: up order is the reverse of down order
    assert started == list(reversed(stopped))


def test_shipped_roster_classifies_obs_svc_as_teardown_workload(tmp_path):
    # the live bug was a ROSTER gap, not a code gap: obs-svc has a running
    # compose project (obs-svc-agg) but was absent from WorkstationConfig, so the
    # lifecycle plan never saw it and `down` orphaned it. this guards the roster
    # itself: feed the shipped WorkstationConfig (with obs-svc's path pointed at a
    # dir that ships a dev-fleet-consuming compose file) through the real
    # _resolve_lifecycle_plan and assert obs-svc is a non-essential workload in
    # the teardown set. mocking the plan (as the other down tests do) cannot catch
    # a roster omission -- only building from the declared roster can.
    import yaml

    from fleet.models import WorkstationConfig

    shipped = _locate_workstation_config()
    with open(shipped) as handle:
        cfg = WorkstationConfig(**yaml.safe_load(handle))

    # obs-svc must be declared in the roster as a non-essential repo.
    obs_repo = next((r for r in cfg.repositories if r.name == "obs-svc"), None)
    assert obs_repo is not None, "obs-svc missing from WorkstationConfig roster"
    assert obs_repo.essential is False

    # repoint every roster repo at a tmp dir so build_plan's compose-file filter
    # sees a compose project regardless of what is checked out on this host. the
    # network owner keeps a dev-fleet-defining file; everyone else consumes it.
    owner_compose = (
        "services:\n  traefik:\n    image: traefik\n"
        "networks:\n  dev-fleet:\n    name: dev-fleet\n    driver: bridge\n"
    )
    dependent_compose = (
        "services:\n  svc:\n    image: busybox\n"
        "networks:\n  dev-fleet:\n    external: true\n"
    )
    repos = []
    for r in cfg.repositories:
        d = tmp_path / r.name
        d.mkdir()
        # fleet itself ships no compose target -- leave it empty so it is excluded
        if r.name != "fleet":
            body = owner_compose if r.name == "traefik" else dependent_compose
            (d / "docker-compose.yml").write_text(body)
        repos.append({"name": r.name, "origin": r.origin, "path": str(d), "essential": r.essential})

    cfg_path = tmp_path / "WorkstationConfig.yaml"
    cfg_path.write_text(WorkstationConfig(
        version=cfg.version, host=cfg.host, repositories=repos, models=cfg.models
    ).model_dump_json())

    plan = _resolve_lifecycle_plan(str(cfg_path))
    obs = next((u for u in plan.units if u.name == "obs-svc"), None)
    assert obs is not None, "obs-svc fell out of the lifecycle plan"
    assert obs.tier == TIER_WORKLOAD
    down = [u.name for u in plan.down_order]
    assert "obs-svc" in down
    assert down.index("obs-svc") < down.index("traefik")

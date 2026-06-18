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
    mock_fetch.return_value = (
        [{"name": "test-repo", "dirty": False, "unpushed": 0, "has_upstream": True, "error": ""}],
        "delightd",
    )
    result = cli_runner.invoke(main, ["sync"])
    assert result.exit_code == 0
    assert "Workstation is clean and safe to teardown" in result.output

@patch("fleet.git_state.fetch_git_state")
def test_sync_dirty(mock_fetch, cli_runner):
    mock_fetch.return_value = (
        [{"name": "dirty-repo", "dirty": True, "unpushed": 2, "has_upstream": True, "error": ""}],
        "delightd",
    )
    result = cli_runner.invoke(main, ["sync"])
    assert result.exit_code == 1
    assert "BLOCKED" in result.output
    assert "dirty-repo" in result.output

@patch("fleet.git_state.fetch_git_state")
def test_sync_unreadable_fails_closed(mock_fetch, cli_runner):
    # A repo whose state could not be verified must block teardown, not pass.
    mock_fetch.return_value = (
        [{"name": "mystery-repo", "dirty": False, "unpushed": 0, "has_upstream": False, "error": "not a git repository"}],
        "local",
    )
    result = cli_runner.invoke(main, ["sync"])
    assert result.exit_code == 1
    assert "BLOCKED" in result.output
    assert "mystery-repo" in result.output


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


@patch("fleet.cli._essential_compose_repos")
@patch("fleet.cli.FleetCore")
def test_bootstrap_starts_config_derived_tier0(mock_core_cls, mock_repos, cli_runner):
    # the message backbone must appear in a cold boot, not just the legacy trio
    mock_repos.return_value = [("traefik", "~/work/traefik"), ("kafka-logging", "~/work/kafka-logging")]
    mock_core = mock_core_cls.return_value
    mock_core.selfcheck = AsyncMock(return_value=[
        SourceHealth(name="docker", reachable=True),
        SourceHealth(name="kube", reachable=False),
    ])
    result = cli_runner.invoke(main, ["bootstrap", "--dry-run"])
    assert result.exit_code == 0, f"Failed with output: {result.output}"
    assert "kafka-logging" in result.output
    assert "traefik" in result.output


@patch("fleet.cli._essential_compose_repos", return_value=[])
@patch("shutil.which", return_value="/opt/homebrew/bin/colima")
@patch("fleet.cli.FleetCore")
def test_bootstrap_prefers_colima_when_docker_down(mock_core_cls, mock_which, mock_repos, cli_runner):
    mock_core = mock_core_cls.return_value
    mock_core.selfcheck = AsyncMock(return_value=[SourceHealth(name="docker", reachable=False)])
    result = cli_runner.invoke(main, ["bootstrap", "--dry-run"])
    assert result.exit_code == 0, f"Failed with output: {result.output}"
    assert "colima" in result.output.lower()
    # empty tier-0 must fall back rather than silently starting nothing
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

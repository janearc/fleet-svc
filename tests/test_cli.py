import pytest
from unittest.mock import patch, MagicMock, AsyncMock, mock_open
from click.testing import CliRunner
from fleet.cli import main
from fleet.models import FleetState, PauseResult

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

@patch("httpx.AsyncClient.get")
@patch("os.path.exists", return_value=True)
@patch("builtins.open", new_callable=MagicMock)
def test_sync_clean(mock_open, mock_exists, mock_get, cli_runner):
    import json
    mock_open.return_value.__enter__.return_value.read.return_value = json.dumps({
        "Repos": [{"Name": "test-repo", "Dirty": False, "Unpushed": 0}]
    })
    result = cli_runner.invoke(main, ["sync"])
    assert result.exit_code == 0
    assert "Workstation is clean and safe to teardown" in result.output

@patch("httpx.AsyncClient.get")
@patch("os.path.exists", return_value=True)
@patch("builtins.open", new_callable=MagicMock)
def test_sync_dirty(mock_open, mock_exists, mock_get, cli_runner):
    import json
    mock_open.return_value.__enter__.return_value.read.return_value = json.dumps({
        "Repos": [{"Name": "dirty-repo", "Dirty": True, "Unpushed": 2}]
    })
    result = cli_runner.invoke(main, ["sync"])
    assert result.exit_code == 1
    assert "BLOCKED" in result.output
    assert "dirty-repo" in result.output

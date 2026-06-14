import pytest
from unittest.mock import patch, MagicMock, AsyncMock
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

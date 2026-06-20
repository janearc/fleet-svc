import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fleet.cli import main
from fleet.deploy import DeployError, deploy
from fleet.git_state import DelightdUnavailable

_CONFIG = """\
version: "1.0"
host:
  os: darwin
  arch: arm64
  daemons: []
repositories:
  - name: paling
    origin: x
    path: "{tmp}"
    essential: false
    deploy:
      kind: launchd
      command: ["echo", "ok"]
  - name: obssvc
    origin: x
    path: "{tmp}"
    deploy:
      kind: kube
      deployment: obs-svc-agg
  - name: nodep
    origin: x
    path: "{tmp}"
"""


@pytest.fixture
def config(tmp_path):
    p = tmp_path / "WorkstationConfig.yaml"
    p.write_text(_CONFIG.format(tmp=tmp_path))
    return str(p)


def _known(name="paling", **git):
    rec = {"name": name, "dirty": False, "unpushed": 0}
    rec.update(git)
    return [rec]


# --- the delightd SOT gate -----------------------------------------------------

def test_unknown_project_refused(config):
    with pytest.raises(DeployError, match="unknown project"):
        deploy("ghost", config_path=config)


@patch("fleet.deploy.fetch_git_state")
def test_no_deploy_descriptor_refused(mock_git, config):
    mock_git.return_value = _known("nodep")
    with pytest.raises(DeployError, match="no deploy descriptor"):
        deploy("nodep", config_path=config)


@patch("fleet.deploy.fetch_git_state")
def test_delightd_unreachable_fails_closed(mock_git, config):
    mock_git.side_effect = DelightdUnavailable("down")
    with pytest.raises(DeployError, match="delightd unreachable"):
        deploy("paling", config_path=config)


@patch("fleet.deploy.fetch_git_state")
def test_unknown_to_delightd_refused(mock_git, config):
    mock_git.return_value = []  # delightd recognises nothing
    with pytest.raises(DeployError, match="does not recognise"):
        deploy("paling", config_path=config)


@patch("fleet.deploy.fetch_git_state")
def test_missing_path_refused(mock_git, config):
    mock_git.return_value = _known("paling", missing_path=True)
    with pytest.raises(DeployError, match="missing on disk"):
        deploy("paling", config_path=config)


@patch("fleet.deploy.fetch_git_state")
def test_unreadable_repo_fails_closed(mock_git, config):
    mock_git.return_value = _known("paling", error="permission denied")
    with pytest.raises(DeployError, match="failing closed"):
        deploy("paling", config_path=config)


# --- the roll ------------------------------------------------------------------

@patch("fleet.deploy.subprocess.run")
@patch("fleet.deploy.fetch_git_state")
def test_launchd_roll_success(mock_git, mock_run, config):
    mock_git.return_value = _known("paling")
    mock_run.return_value = MagicMock(returncode=0, stdout="loaded", stderr="")
    result = deploy("paling", config_path=config)
    assert result.rolled and result.kind == "launchd" and result.delightd_known
    mock_run.assert_called_once()


@patch("fleet.deploy.subprocess.run")
@patch("fleet.deploy.fetch_git_state")
def test_kube_roll_success(mock_git, mock_run, config):
    mock_git.return_value = _known("obssvc")
    mock_run.return_value = MagicMock(returncode=0, stdout="restarted", stderr="")
    result = deploy("obssvc", config_path=config)
    assert result.kind == "kube" and result.rolled
    assert mock_run.call_args[0][0][:3] == ["kubectl", "rollout", "restart"]


@patch("fleet.deploy.subprocess.run")
@patch("fleet.deploy.fetch_git_state")
def test_dry_run_does_not_act(mock_git, mock_run, config):
    mock_git.return_value = _known("paling")
    result = deploy("paling", config_path=config, dry_run=True)
    assert result.dry_run and not result.rolled and "would run" in result.detail
    mock_run.assert_not_called()


@patch("fleet.deploy.subprocess.run")
@patch("fleet.deploy.fetch_git_state")
def test_dirty_tree_warns_but_proceeds(mock_git, mock_run, config):
    mock_git.return_value = _known("paling", dirty=True, unpushed=2)
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    result = deploy("paling", config_path=config)
    assert result.rolled
    assert any("uncommitted" in w for w in result.warnings)
    assert any("unpushed" in w for w in result.warnings)


@patch("fleet.deploy.subprocess.run")
@patch("fleet.deploy.fetch_git_state")
def test_roll_failure_raises(mock_git, mock_run, config):
    mock_git.return_value = _known("paling")
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
    with pytest.raises(DeployError, match="launchd roll failed"):
        deploy("paling", config_path=config)


# --- the CLI surface -----------------------------------------------------------

@patch("fleet.deploy.subprocess.run")
@patch("fleet.deploy.fetch_git_state")
def test_cli_deploy_json(mock_git, mock_run, config):
    mock_git.return_value = _known("paling")
    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
    res = CliRunner().invoke(main, ["deploy", "paling", "--config", config, "--json"])
    assert res.exit_code == 0
    assert json.loads(res.output)["project"] == "paling"


@patch("fleet.deploy.fetch_git_state")
def test_cli_deploy_refusal_exits_nonzero(mock_git, config):
    mock_git.return_value = []
    res = CliRunner().invoke(main, ["deploy", "paling", "--config", config])
    assert res.exit_code == 1
    assert "does not recognise" in res.output

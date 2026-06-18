from unittest.mock import MagicMock, patch

from fleet.git_state import fetch_git_state


@patch("fleet.git_state.httpx.Client")
def test_fetch_delightd_flattens_project_git(mock_client_cls):
    # delightd returns git as an element of a project; fleet flattens it.
    client = mock_client_cls.return_value.__enter__.return_value
    resp = MagicMock()
    resp.json.return_value = {
        "status": "ok",
        "projects": [
            {"name": "paling", "git": {"branch": "main", "dirty": True, "unpushed": 0,
                                       "has_upstream": True, "remote_url": "x", "error": ""}},
        ],
    }
    client.get.return_value = resp

    repos, source = fetch_git_state([])
    assert source == "delightd"
    assert repos == [{"name": "paling", "branch": "main", "dirty": True, "unpushed": 0,
                      "has_upstream": True, "remote_url": "x", "error": ""}]


@patch("fleet.git_state.httpx.Client")
def test_fetch_falls_back_to_local_when_daemon_down(mock_client_cls):
    # Daemon unreachable -> local fallback over the roster (empty here).
    mock_client_cls.return_value.__enter__.return_value.get.side_effect = Exception("down")
    repos, source = fetch_git_state([])
    assert source == "local"
    assert repos == []

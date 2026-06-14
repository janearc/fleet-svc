import pytest
from unittest.mock import MagicMock, AsyncMock
from fleet.server import create_app
from fastapi.testclient import TestClient
from fleet.models import PauseResult

@pytest.fixture
def mock_core(sample_fleet_state, sample_source_health, sample_services):
    core = MagicMock()
    core.show = AsyncMock(return_value=sample_fleet_state)
    core.selfcheck = AsyncMock(return_value=sample_source_health)
    
    pause_res = PauseResult(action="pause", dry_run=True, affected=[], skipped=[], errors=[])
    core.pause = AsyncMock(return_value=pause_res)
    core.resume = AsyncMock(return_value=pause_res)
    return core

@pytest.fixture
def mock_auth():
    auth = MagicMock()
    auth.verify_signature.return_value = True
    return auth

def test_healthz(mock_core, mock_auth):
    app = create_app(mock_core, mock_auth)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

def test_metrics(mock_core, mock_auth):
    app = create_app(mock_core, mock_auth)
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "fleet_up 1" in resp.text

def test_show_api(mock_core, mock_auth):
    app = create_app(mock_core, mock_auth)
    client = TestClient(app)
    resp = client.get("/api/show")
    assert resp.status_code == 200
    assert len(resp.json()["services"]) == 3

def test_pause_api_no_auth(mock_core, mock_auth):
    app = create_app(mock_core, mock_auth)
    client = TestClient(app)
    resp = client.post("/api/pause")
    assert resp.status_code == 401 # Missing auth headers

def test_pause_api_with_auth(mock_core, mock_auth):
    app = create_app(mock_core, mock_auth)
    client = TestClient(app)
    resp = client.post("/api/pause", headers={"X-Fleet-Sig": "sig", "X-Fleet-Nonce": "nonce"})
    assert resp.status_code == 200

import pytest
from fleet.core import FleetCore
from unittest.mock import patch, MagicMock, AsyncMock

@pytest.mark.asyncio
async def test_fleet_core_init():
    with patch("fleet.core.DockerSource"), \
         patch("fleet.core.KubeSource"), \
         patch("fleet.core.DelightdSource"), \
         patch("fleet.core.TraefikSource"), \
         patch("fleet.core.EnvoySource"), \
         patch("fleet.core.TransparentSource"):
        core = FleetCore()
        assert len(core.sources) == 6

@pytest.mark.asyncio
async def test_fleet_core_models():
    core = FleetCore()
    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"sources": [{"provider": "ollama", "models": ["test-model"]}]}
        mock_get.return_value = mock_resp
        
        models = await core.models()
        assert len(models) == 1
        assert models[0]["provider"] == "ollama"

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.side_effect = Exception("network error")
        models = await core.models()
        assert len(models) == 0

@pytest.mark.asyncio
async def test_fleet_core_selfcheck():
    core = FleetCore()
    # Mock the sources list to have a fake source
    mock_source = MagicMock()
    mock_source.healthy = AsyncMock()
    core.sources = [mock_source]
    
    await core.selfcheck()
    mock_source.healthy.assert_called_once()

@pytest.mark.asyncio
async def test_fleet_core_show():
    from fleet.models import ServiceRecord, SourceHealth
    core = FleetCore()
    mock_source = MagicMock()
    mock_source.name = "docker"
    mock_source.collect = AsyncMock(return_value=[
        ServiceRecord(name="web", source="docker", status="running")
    ])
    mock_source.healthy = AsyncMock(return_value=SourceHealth(name="docker", reachable=True))
    core.sources = [mock_source]
    core._journal = MagicMock()
    core._journal.get_paused_services.return_value = [{"service_name": "web"}]
    
    state = await core.show()
    assert len(state.services) == 1
    assert state.services[0].name == "web"
    assert state.services[0].paused_by_fleet is True
    assert len(state.sources) == 1

@pytest.mark.asyncio
async def test_fleet_core_show_status_filter(sample_services):
    from fleet.core import FleetCore
    core = FleetCore()
    
    # Mock source to return specific services
    mock_source = MagicMock()
    mock_source.name = "test"
    
    s_healthy = sample_services[0]
    s_healthy.status = "running"
    s_healthy.diagnostics = {}
    
    s_unhealthy = sample_services[1]
    s_unhealthy.status = "error"
    s_unhealthy.diagnostics = {}
    
    s_questionable = sample_services[2]
    s_questionable.status = "running"
    s_questionable.diagnostics = {"questionable": True}
    
    mock_source.collect = AsyncMock(return_value=[s_healthy, s_unhealthy, s_questionable])
    from fleet.models import SourceHealth
    mock_source.healthy = AsyncMock(return_value=SourceHealth(name="test", reachable=True))
    
    core.sources = [mock_source]
    
    # Check healthy
    state = await core.show(status_filter="healthy")
    assert len(state.services) == 1
    assert state.services[0].name == s_healthy.name
    
    # Check unhealthy
    state = await core.show(status_filter="unhealthy")
    assert len(state.services) == 2
    assert state.services[0].name == s_unhealthy.name
    
    # Check questionable
    state = await core.show(status_filter="questionable")
    assert len(state.services) == 1
    assert state.services[0].name == s_questionable.name

@pytest.mark.asyncio
async def test_fleet_core_pause_resume():
    from fleet.models import PauseResult
    core = FleetCore()
    core.show = AsyncMock(return_value=MagicMock(services=[]))
    core._pause_manager = MagicMock()
    core._pause_manager.execute_pause = AsyncMock(return_value=PauseResult(action="pause", dry_run=False, affected=[], skipped=[], errors=[]))
    core._pause_manager.execute_resume = AsyncMock(return_value=PauseResult(action="resume", dry_run=False, affected=[], skipped=[], errors=[]))
    
    await core.pause()
    core._pause_manager.execute_pause.assert_called_once()
    
    await core.resume()
    core._pause_manager.execute_resume.assert_called_once()

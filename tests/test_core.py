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

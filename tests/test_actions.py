import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fleet.actions import PauseManager
from fleet.state import PauseJournal
from fleet.models import ServiceRecord

@pytest.fixture
def mock_journal():
    journal = MagicMock(spec=PauseJournal)
    journal.get_paused_services.return_value = []
    journal.get_stale_intents.return_value = []
    return journal

@pytest.mark.asyncio
async def test_execute_pause_skips_essential(mock_journal):
    manager = PauseManager(mock_journal)
    services = [
        ServiceRecord(name="essential-svc", source="docker", status="running", essential=True),
        ServiceRecord(name="normal-svc", source="docker", status="running", essential=False, metadata={"docker_client": MagicMock()})
    ]
    
    # Mock internal pause functions
    manager.pause_docker = AsyncMock()
    manager._get_docker_client = MagicMock(return_value=MagicMock())
    
    res = await manager.execute_pause(services, dry_run=False)
    
    assert len(res.skipped) == 1
    assert res.skipped[0].name == "essential-svc"
    assert len(res.affected) == 1
    assert res.affected[0].name == "normal-svc"
    
    manager.pause_docker.assert_called_once()

@pytest.mark.asyncio
async def test_execute_pause_dry_run(mock_journal):
    manager = PauseManager(mock_journal)
    services = [
        ServiceRecord(name="normal-svc", source="docker", status="running", essential=False)
    ]
    
    manager.pause_docker = AsyncMock()
    
    res = await manager.execute_pause(services, dry_run=True)
    
    assert res.dry_run is True
    assert len(res.affected) == 1
    assert len(res.skipped) == 0
    manager.pause_docker.assert_not_called()

@pytest.mark.asyncio
async def test_execute_pause_real(mock_journal):
    manager = PauseManager(mock_journal)
    services = [
        ServiceRecord(name="docker-svc", source="docker", status="running", metadata={"docker_client": MagicMock()}),
        ServiceRecord(name="kube-svc", source="kube", status="running", metadata={"k8s_client": MagicMock()}),
        ServiceRecord(name="other-svc", source="delightd", status="running")
    ]
    
    manager.pause_docker = AsyncMock()
    manager.pause_kube = AsyncMock()
    
    res = await manager.execute_pause(services, dry_run=False)
    
    assert res.dry_run is False
    assert len(res.affected) == 3
    manager.pause_docker.assert_called_once()
    manager.pause_kube.assert_called_once()

@pytest.mark.asyncio
async def test_execute_resume(mock_journal):
    manager = PauseManager(mock_journal)
    
    mock_journal.get_paused_services.return_value = [
        {"service_name": "docker-svc", "source": "docker", "prev_state": "{}"},
        {"service_name": "kube-svc", "source": "kube", "prev_state": "{}"},
    ]
    
    manager.resume_docker = AsyncMock()
    manager.resume_kube = AsyncMock()
    
    res = await manager.execute_resume(dry_run=True)
    assert len(res.affected) == 2
    manager.resume_docker.assert_not_called()

@pytest.mark.asyncio
async def test_pause_docker(mock_journal):
    manager = PauseManager(mock_journal)
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_container.name = "test-container"
    mock_container.status = "running"
    mock_container.labels = {}
    mock_container.image.tags = ["nginx:latest"]
    mock_container.ports = {"80/tcp": [{"HostPort": "8080"}]}
    mock_client.containers.get.return_value = mock_container
    
    await manager.pause_docker("test-container", mock_client)
    mock_container.stop.assert_called_once()
    mock_journal.record_intent.assert_called_once()

@pytest.mark.asyncio
async def test_resume_docker(mock_journal):
    manager = PauseManager(mock_journal)
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.get.return_value = mock_container
    
    await manager.resume_docker("test-container", mock_client)
    mock_container.start.assert_called_once()
    mock_journal.mark_resumed.assert_called_once()

@pytest.mark.asyncio
async def test_pause_kube(mock_journal):
    manager = PauseManager(mock_journal)
    mock_client = MagicMock()
    mock_deployment = MagicMock()
    mock_deployment.spec.replicas = 3
    mock_client.read_namespaced_deployment.return_value = mock_deployment
    
    await manager.pause_kube("test-deploy", "default", mock_client)
    mock_client.patch_namespaced_deployment.assert_called_once()
    mock_journal.record_intent.assert_called_once()

@pytest.mark.asyncio
async def test_resume_kube(mock_journal):
    manager = PauseManager(mock_journal)
    mock_client = MagicMock()
    mock_deployment = MagicMock()
    mock_deployment.metadata.annotations = {"fleet.prev-replicas": "3"}
    mock_client.read_namespaced_deployment.return_value = mock_deployment
    
    await manager.resume_kube("test-deploy", "default", mock_client)
    mock_client.patch_namespaced_deployment.assert_called_once()
    mock_journal.mark_resumed.assert_called_once()


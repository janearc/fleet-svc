import pytest
from unittest.mock import MagicMock, patch
from fleet.sources.delightd import DelightdSource
from fleet.sources.traefik import TraefikSource

@pytest.mark.asyncio
async def test_delightd_source_healthy(respx_mock):
    respx_mock.get("http://localhost:8080/health").respond(json={"status": "ok"})
    source = DelightdSource()
    health = await source.healthy()
    assert health.reachable is True

@pytest.mark.asyncio
async def test_delightd_source_collect(respx_mock):
    respx_mock.get("http://localhost:8080/health").respond(json={"active_projects": 1})
    respx_mock.get("http://localhost:8080/projects/default/state").respond(json={"state": "monitoring"})
    
    source = DelightdSource(project_names=["default"])
    services = await source.collect()
    assert len(services) == 1
    assert services[0].name == "default"
    assert services[0].status == "running"

@pytest.mark.asyncio
async def test_traefik_source_healthy(respx_mock):
    respx_mock.get("http://localhost:8081/api/overview").respond(json={})
    source = TraefikSource()
    health = await source.healthy()
    assert health.reachable is True

@pytest.mark.asyncio
async def test_traefik_source_collect(respx_mock):
    respx_mock.get("http://localhost:8081/api/http/routers").respond(json=[
        {"name": "web@docker", "status": "enabled", "rule": "Host(`test.local`)", "service": "web"}
    ])
    respx_mock.get("http://localhost:8081/api/http/services").respond(json=[
        {"name": "web", "loadBalancer": {"servers": [{"url": "http://10.0.0.1:80"}]}}
    ])
    
    source = TraefikSource()
    services = await source.collect()
    assert len(services) == 1
    assert services[0].name == "web"
    assert services[0].status == "routed"
    assert services[0].metadata["server_urls"] == ["http://10.0.0.1:80"]

@pytest.mark.asyncio
async def test_envoy_source(respx_mock):
    from fleet.sources.envoy import EnvoySource
    respx_mock.get("http://localhost:9901/clusters").respond(text="cluster_name::status::health_flags::fail")
    respx_mock.get("http://localhost:9901/ready").respond(text="ready")
    source = EnvoySource()
    health = await source.healthy()
    assert health.reachable is True
    services = await source.collect()
    assert len(services) == 1
    assert services[0].name == "cluster_name"
    assert services[0].status == "error"

@pytest.mark.asyncio
async def test_docker_source():
    from fleet.sources.docker_source import DockerSource
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    
    mock_container = MagicMock()
    mock_container.name = "web"
    mock_container.image.tags = ["nginx:latest"]
    mock_container.status = "running"
    mock_container.labels = {"fleet.essential": "true", "fleet.paused": "false"}
    mock_container.ports = {"80/tcp": [{"HostPort": "8080"}]}
    mock_container.attrs = {"State": {"StartedAt": "2023-01-01T00:00:00Z"}}
    mock_client.containers.list.return_value = [mock_container]
    
    source = DockerSource(client=mock_client)
    health = await source.healthy()
    assert health.reachable is True
    services = await source.collect()
    assert len(services) == 1
    assert services[0].essential is True

@pytest.mark.asyncio
async def test_kube_source():
    from fleet.sources.kube_source import KubeSource
    mock_client = MagicMock()
    
    mock_item = MagicMock()
    mock_item.metadata.name = "web"
    mock_item.metadata.namespace = "default"
    mock_item.metadata.labels = {"fleet.essential": "true"}
    mock_item.metadata.annotations = {"fleet.paused": "false", "fleet.prev-replicas": "3"}
    mock_item.spec.replicas = 3
    mock_item.status.available_replicas = 3
    mock_item.status.ready_replicas = 3
    
    container = MagicMock()
    container.image = "nginx:latest"
    mock_item.spec.template.spec.containers = [container]
    
    mock_client.list_namespaced_deployment.return_value.items = [mock_item]
    
    source = KubeSource(apps_v1=mock_client)
    services = await source.collect()
    assert len(services) == 1
    assert services[0].essential is True


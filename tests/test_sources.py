import pytest
from unittest.mock import MagicMock, patch
from fleet.sources.delightd import DelightdSource
from fleet.sources.traefik import TraefikSource

@pytest.mark.asyncio
async def test_delightd_source_healthy(respx_mock):
    # _resolve_host first probes Traefik's routers; an empty list means no delightd
    # route is published, so it falls back to the direct control port 127.0.0.1:8088.
    respx_mock.get("http://127.0.0.1:8080/api/http/routers").respond(json=[])
    respx_mock.get("http://127.0.0.1:8088/health").respond(json={"status": "ok"})
    source = DelightdSource()
    health = await source.healthy()
    assert health.reachable is True

@pytest.mark.asyncio
async def test_delightd_source_collect(respx_mock):
    respx_mock.get("http://127.0.0.1:8080/api/http/routers").respond(json=[])
    respx_mock.get("http://127.0.0.1:8088/health").respond(json={"active_projects": 1})
    respx_mock.get("http://127.0.0.1:8088/projects/default/state").respond(json={"state": "monitoring"})

    source = DelightdSource(project_names=["default"])
    services = await source.collect()
    assert len(services) == 1
    assert services[0].name == "default"
    assert services[0].status == "running"

@pytest.mark.asyncio
async def test_traefik_source_healthy(respx_mock):
    # TraefikSource defaults to the Traefik API on :8080 (its well-known port).
    respx_mock.get("http://localhost:8080/api/overview").respond(json={})
    source = TraefikSource()
    health = await source.healthy()
    assert health.reachable is True

@pytest.mark.asyncio
async def test_traefik_source_collect(respx_mock):
    respx_mock.get("http://localhost:8080/api/http/routers").respond(json=[
        {"name": "web@docker", "status": "enabled", "rule": "Host(`test.local`)", "service": "web"}
    ])
    respx_mock.get("http://localhost:8080/api/http/services").respond(json=[
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


def test_resolve_base_url_honours_docker_host(monkeypatch):
    from fleet.sources import docker_source

    # an explicit DOCKER_HOST always wins, regardless of context.
    monkeypatch.setenv("DOCKER_HOST", "tcp://1.2.3.4:2375")
    assert docker_source._resolve_base_url() == "tcp://1.2.3.4:2375"


def test_resolve_base_url_follows_active_context(monkeypatch):
    from fleet.sources import docker_source

    monkeypatch.delenv("DOCKER_HOST", raising=False)

    fake_ctx = MagicMock()
    fake_ctx.endpoints = {"docker": {"Host": "unix:///tmp/ctx.sock"}}
    with patch("docker.context.ContextAPI.get_current_context", return_value=fake_ctx):
        assert docker_source._resolve_base_url() == "unix:///tmp/ctx.sock"


def test_resolve_base_url_falls_back_to_colima_socket(monkeypatch, tmp_path):
    from fleet.sources import docker_source

    monkeypatch.delenv("DOCKER_HOST", raising=False)

    # no usable context, but the colima default socket exists on disk.
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    fake_ctx = MagicMock()
    fake_ctx.endpoints = {}
    with patch("docker.context.ContextAPI.get_current_context", return_value=fake_ctx), \
         patch.object(docker_source, "_COLIMA_DEFAULT_SOCK", sock):
        assert docker_source._resolve_base_url() == f"unix://{sock}"


def test_resolve_base_url_returns_none_when_nothing_resolves(monkeypatch, tmp_path):
    from fleet.sources import docker_source

    monkeypatch.delenv("DOCKER_HOST", raising=False)

    fake_ctx = MagicMock()
    fake_ctx.endpoints = {}
    with patch("docker.context.ContextAPI.get_current_context", return_value=fake_ctx), \
         patch.object(docker_source, "_COLIMA_DEFAULT_SOCK", tmp_path / "missing.sock"):
        assert docker_source._resolve_base_url() is None


def test_resolve_base_url_survives_context_failure(monkeypatch, tmp_path):
    from fleet.sources import docker_source

    monkeypatch.delenv("DOCKER_HOST", raising=False)

    # context lookup blowing up must not propagate — it degrades to the
    # socket fallback (here absent), yielding None.
    with patch("docker.context.ContextAPI.get_current_context", side_effect=RuntimeError("boom")), \
         patch.object(docker_source, "_COLIMA_DEFAULT_SOCK", tmp_path / "missing.sock"):
        assert docker_source._resolve_base_url() is None


def test_docker_source_uses_resolved_base_url(monkeypatch):
    from fleet.sources import docker_source
    from fleet.sources.docker_source import DockerSource

    monkeypatch.setattr(docker_source, "_resolve_base_url", lambda: "unix:///tmp/resolved.sock")
    with patch("docker.DockerClient") as mock_dc:
        DockerSource()
        mock_dc.assert_called_once_with(base_url="unix:///tmp/resolved.sock")


def test_docker_source_falls_back_to_from_env(monkeypatch):
    from fleet.sources import docker_source
    from fleet.sources.docker_source import DockerSource

    # nothing resolves -> let the docker lib try its own defaults.
    monkeypatch.setattr(docker_source, "_resolve_base_url", lambda: None)
    with patch("docker.from_env") as mock_from_env:
        DockerSource()
        mock_from_env.assert_called_once()


@pytest.mark.asyncio
async def test_docker_source_collect_degrades_on_list_failure():
    from fleet.sources.docker_source import DockerSource

    # the daemon went away between init and collect: list() raises, and the
    # source degrades to an empty result rather than propagating.
    mock_client = MagicMock()
    mock_client.containers.list.side_effect = Exception("connection refused")
    source = DockerSource(client=mock_client)
    assert await source.collect() == []


@pytest.mark.asyncio
async def test_docker_source_deployment_from_image_and_name_strip():
    from fleet.sources.docker_source import DockerSource

    # no compose-project label -> deployment derived from the image name;
    # a leading slash on the container name is stripped.
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_container.name = "/standalone"
    mock_container.image.tags = ["ghcr.io/acme/widget:1.2"]
    mock_container.status = "exited"
    mock_container.labels = {}
    mock_container.ports = {}
    mock_container.attrs = {"State": {}}
    mock_client.containers.list.return_value = [mock_container]

    source = DockerSource(client=mock_client)
    services = await source.collect()
    assert len(services) == 1
    assert services[0].name == "standalone"
    assert services[0].status == "stopped"
    assert services[0].deployment == "widget"


@pytest.mark.asyncio
async def test_docker_source_degrades_when_client_init_raises(monkeypatch):
    from fleet.sources import docker_source
    from fleet.sources.docker_source import DockerSource

    # a missing socket / unavailable daemon must not crash; the source comes
    # up with a None client and reports unreachable.
    monkeypatch.setattr(docker_source, "_resolve_base_url", lambda: "unix:///tmp/nope.sock")
    with patch("docker.DockerClient", side_effect=Exception("no daemon")):
        source = DockerSource()
    assert source._client is None
    health = await source.healthy()
    assert health.reachable is False
    assert health.error == "docker client unavailable"
    # collect on a dead client degrades to an empty list, not a crash.
    assert await source.collect() == []

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


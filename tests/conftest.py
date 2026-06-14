import pytest
from datetime import datetime
from fleet.models import ServiceRecord, FleetState, SourceHealth

@pytest.fixture
def sample_services():
    return [
        ServiceRecord(
            name="delightd",
            source="docker",
            status="running",
            essential=True,
            uptime="2d",
            metadata={"labels": {"fleet.essential": "true"}}
        ),
        ServiceRecord(
            name="app-web",
            source="kube",
            status="running",
            replicas=2,
            namespace="default"
        ),
        ServiceRecord(
            name="app-worker",
            source="kube",
            status="paused",
            paused_by_fleet=True,
            replicas=0,
            prev_replicas=3,
            namespace="default"
        )
    ]

@pytest.fixture
def sample_source_health():
    return [
        SourceHealth(name="docker", reachable=True, latency_ms=5.0),
        SourceHealth(name="kube", reachable=False, error="Connection refused")
    ]

@pytest.fixture
def sample_fleet_state(sample_services, sample_source_health):
    return FleetState(
        services=sample_services,
        sources=sample_source_health,
        collected_at=datetime.now()
    )

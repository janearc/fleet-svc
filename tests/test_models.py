import pytest
from datetime import datetime
from fleet.models import ServiceRecord, SourceHealth, FleetState, PauseResult

def test_service_record_creation():
    r = ServiceRecord(
        name="web",
        source="docker",
        status="running",
        essential=True
    )
    assert r.name == "web"
    assert r.source == "docker"
    assert r.status == "running"
    assert r.essential is True
    assert r.paused_by_fleet is False
    assert r.ports == []
    assert r.diagnostics == {}

def test_fleet_state(sample_services, sample_source_health):
    state = FleetState(
        services=sample_services,
        sources=sample_source_health,
        collected_at=datetime.now()
    )
    assert len(state.services) == 3
    assert len(state.sources) == 2

def test_pause_result(sample_services):
    res = PauseResult(
        action="pause",
        dry_run=True,
        affected=[sample_services[1]],
        skipped=[sample_services[0]],
        errors=[{"name": "app-broken", "error": "timeout"}]
    )
    assert res.action == "pause"
    assert res.dry_run is True
    assert len(res.affected) == 1
    assert len(res.skipped) == 1
    assert len(res.errors) == 1

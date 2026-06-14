import pytest
from fleet.diagnostics import DiagnosticsCollector
from fleet.models import ServiceRecord
from unittest.mock import patch, AsyncMock

@pytest.mark.asyncio
async def test_diagnose_service_no_metrics():
    collector = DiagnosticsCollector()
    svc = ServiceRecord(name="web", source="docker", status="running", metadata={})
    res = await collector.diagnose_service(svc)
    assert res == {}

@pytest.mark.asyncio
async def test_diagnose_service_with_metrics(respx_mock):
    respx_mock.get("http://127.0.0.1:9090/metrics").respond(text='''
# HELP process_resident_memory_bytes
# TYPE process_resident_memory_bytes gauge
process_resident_memory_bytes 100000000000.0
up 0
http_requests_total{status="500"} 10
http_requests_total{status="200"} 50
''')
    collector = DiagnosticsCollector()
    svc = ServiceRecord(name="web", source="docker", status="running", metadata={"metrics_port": 9090})
    
    res = await collector.diagnose_service(svc)
    assert res.get("memory_high") is True
    assert res["down"] is True
    assert "error_rate" in res

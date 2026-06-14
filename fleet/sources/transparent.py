import logging
import httpx
from datetime import datetime
from fleet.sources.base import Source
from fleet.models import ServiceRecord, SourceHealth

logger = logging.getLogger(__name__)

class TransparentSource(Source):
    name = "transparent"

    def __init__(self, url: str = "http://localhost:8081", timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    async def collect(self) -> list[ServiceRecord]:
        services = []
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # Try getting the dashboard or any data endpoint
                # Since we know transparent serves prometheus-style metrics and potentially data.json
                # For now we'll query /metrics and extract some data or try a mock /data.json
                # Fallback to GET /dashboard and parse lines
                resp = await client.get(f"{self.url}/metrics")
                resp.raise_for_status()
                text = resp.text
                
                # Basic parsing to extract some known services if possible
                # If transparent exposes structured data, this should be updated to use that endpoint
                # Assuming /metrics might have some series with 'project' or 'service' labels
                project_names = set()
                for line in text.splitlines():
                    if line.startswith("#"):
                        continue
                    if 'project="' in line:
                        parts = line.split('project="')
                        if len(parts) > 1:
                            proj = parts[1].split('"')[0]
                            project_names.add(proj)

                for name in project_names:
                    services.append(ServiceRecord(
                        name=name,
                        source=self.name,
                        status="running",
                        metadata={"info": "discovered via transparent metrics"}
                    ))
        except Exception as e:
            logger.warning("Transparent collection failed", extra={"error": str(e), "url": self.url})
        return services

    async def healthy(self) -> SourceHealth:
        try:
            start_time = datetime.now()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.url}/metrics")
                resp.raise_for_status()
            latency_ms = (datetime.now() - start_time).total_seconds() * 1000
            return SourceHealth(name=self.name, reachable=True, latency_ms=latency_ms)
        except Exception as e:
            return SourceHealth(name=self.name, reachable=False, error=str(e))

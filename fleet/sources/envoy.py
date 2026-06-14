import logging
import httpx
from datetime import datetime
from fleet.sources.base import Source
from fleet.models import ServiceRecord, SourceHealth

logger = logging.getLogger(__name__)

class EnvoySource(Source):
    name = "envoy"

    def __init__(self, url: str = "http://localhost:9901", timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    async def collect(self) -> list[ServiceRecord]:
        services = []
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.url}/clusters")
                resp.raise_for_status()
                text = resp.text
                
                # Parse envoy text output for clusters
                # Output lines often look like: cluster_name::status...
                lines = text.splitlines()
                clusters = set()
                health_map = {}
                
                for line in lines:
                    if "::" in line:
                        parts = line.split("::")
                        cluster_name = parts[0]
                        clusters.add(cluster_name)
                        
                        if "health_flags" in line:
                            # Try to extract health info
                            health_info = line.split("health_flags::")[1] if "health_flags::" in line else ""
                            health_map[cluster_name] = "error" if "fail" in health_info.lower() else "routed"

                for cluster_name in clusters:
                    status = health_map.get(cluster_name, "routed")
                    services.append(ServiceRecord(
                        name=cluster_name,
                        source=self.name,
                        status=status,
                        metadata={}
                    ))
        except Exception as e:
            logger.warning("Envoy collection failed", extra={"error": str(e), "url": self.url})
        return services

    async def healthy(self) -> SourceHealth:
        try:
            start_time = datetime.now()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.url}/ready")
                resp.raise_for_status()
            latency_ms = (datetime.now() - start_time).total_seconds() * 1000
            return SourceHealth(name=self.name, reachable=True, latency_ms=latency_ms)
        except Exception as e:
            return SourceHealth(name=self.name, reachable=False, error=str(e))

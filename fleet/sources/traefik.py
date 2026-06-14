import logging
import httpx
from datetime import datetime
from fleet.sources.base import Source
from fleet.models import ServiceRecord, SourceHealth

logger = logging.getLogger(__name__)

class TraefikSource(Source):
    name = "traefik"

    def __init__(self, url: str = "http://localhost:8080", timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    async def collect(self) -> list[ServiceRecord]:
        services = []
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                routers_resp = await client.get(f"{self.url}/api/http/routers")
                routers_resp.raise_for_status()
                routers_data = routers_resp.json()

                services_resp = await client.get(f"{self.url}/api/http/services")
                services_resp.raise_for_status()
                services_data = services_resp.json()

                # Map services data for easy lookup
                services_map = {svc["name"]: svc for svc in services_data if "name" in svc}

                for router in routers_data:
                    name = router.get("name", "unknown")
                    # Traefik names often have @provider, we might want to strip it for cleaner matching
                    clean_name = name.split("@")[0] if "@" in name else name
                    
                    rule = router.get("rule", "")
                    service_name = router.get("service", "")
                    
                    metadata = {"rule": rule, "router_name": name, "provider": router.get("provider", "unknown")}
                    
                    if service_name in services_map:
                        svc_info = services_map[service_name]
                        urls = []
                        if "loadBalancer" in svc_info and "servers" in svc_info["loadBalancer"]:
                            urls = [s.get("url") for s in svc_info["loadBalancer"]["servers"] if "url" in s]
                        metadata["server_urls"] = urls

                    status = "routed" if router.get("status") == "enabled" else "error"

                    services.append(ServiceRecord(
                        name=clean_name,
                        source=self.name,
                        status=status,
                        metadata=metadata
                    ))
        except Exception as e:
            logger.warning("Traefik collection failed", extra={"error": str(e), "url": self.url})
        return services

    async def healthy(self) -> SourceHealth:
        try:
            start_time = datetime.now()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.url}/api/overview")
                resp.raise_for_status()
            latency_ms = (datetime.now() - start_time).total_seconds() * 1000
            return SourceHealth(name=self.name, reachable=True, latency_ms=latency_ms)
        except Exception as e:
            return SourceHealth(name=self.name, reachable=False, error=str(e))

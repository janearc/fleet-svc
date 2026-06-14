import logging
import re
import httpx
from datetime import datetime
from fleet.sources.base import Source
from fleet.models import ServiceRecord, SourceHealth

logger = logging.getLogger(__name__)

class TransparentSource(Source):
    name = "transparent"

    def __init__(self, url: str | None = None, timeout: float = 5.0):
        self._url = url
        self.timeout = timeout
        self._host_header = None

    async def _resolve_host(self) -> tuple[str, str | None]:
        if self._url:
            return self._url, None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get("http://127.0.0.1:8080/api/http/routers")
                if resp.status_code == 200:
                    routers = resp.json()
                    for r in routers:
                        if r.get("name", "").startswith("transparent"):
                            rule = r.get("rule", "")
                            m = re.search(r"Host\(`([^`]+)`\)", rule)
                            if m:
                                hostname = m.group(1)
                                return "http://127.0.0.1:80", hostname
        except Exception as e:
            logger.debug(f"Failed to resolve transparent hostname from Traefik: {e}")
        return "http://127.0.0.1:8080", None # Fallback

    async def collect(self) -> list[ServiceRecord]:
        services = []
        try:
            url, host_header = await self._resolve_host()
            headers = {"Host": host_header} if host_header else {}
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{url}/metrics", headers=headers)
                resp.raise_for_status()
                text = resp.text
                
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
            logger.warning("Transparent collection failed", extra={"error": str(e), "url": getattr(self, "url", "unknown")})
        return services

    async def healthy(self) -> SourceHealth:
        try:
            start_time = datetime.now()
            url, host_header = await self._resolve_host()
            headers = {"Host": host_header} if host_header else {}
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{url}/metrics", headers=headers)
                resp.raise_for_status()
            latency_ms = (datetime.now() - start_time).total_seconds() * 1000
            return SourceHealth(name=self.name, reachable=True, latency_ms=latency_ms)
        except Exception as e:
            return SourceHealth(name=self.name, reachable=False, error=str(e))

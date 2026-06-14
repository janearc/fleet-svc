import logging
import httpx
from datetime import datetime
from fleet.sources.base import Source
from fleet.models import ServiceRecord, SourceHealth

logger = logging.getLogger(__name__)

class OdysseusSource(Source):
    name = "odysseus"

    def __init__(self, port: int = 7860, timeout: float = 2.0):
        self.port = port
        self.timeout = timeout
        self.url = f"http://127.0.0.1:{self.port}"

    async def collect(self) -> list[ServiceRecord]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # We do a HEAD request or GET to see if it responds.
                # Odysseus redirects / to /login.
                resp = await client.get(self.url, follow_redirects=False)
                # Any successful connection means it's running
                status = "running"
        except Exception:
            status = "stopped"

        return [
            ServiceRecord(
                name="odysseus-native",
                source=self.name,
                status=status,
                image="native (python)",
                deployment="odysseus",
                metadata={"info": f"monitored via {self.url}"}
            )
        ]

    async def healthy(self) -> SourceHealth:
        try:
            start_time = datetime.now()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await client.get(self.url, follow_redirects=False)
            latency_ms = (datetime.now() - start_time).total_seconds() * 1000
            return SourceHealth(name=self.name, reachable=True, latency_ms=latency_ms)
        except Exception as e:
            return SourceHealth(name=self.name, reachable=False, error=str(e))

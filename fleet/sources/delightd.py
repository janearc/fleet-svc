from __future__ import annotations

import logging
import time

import httpx

from fleet.models import ServiceRecord, SourceHealth
from fleet.sources.base import Source

log = logging.getLogger(__name__)

# delightd state -> fleet normalized status
_STATE_MAP: dict[str, str] = {
    "fallow": "running",
    "monitoring": "running",
    "backing_up": "running",
    "error": "error",
}

_DEFAULT_TIMEOUT = 5.0


class DelightdSource(Source):
    name = "delightd"

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        project_names: list[str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._project_names = project_names or []
        self._timeout = timeout

    async def collect(self) -> list[ServiceRecord]:
        records: list[ServiceRecord] = []
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            ) as client:
                # validate connectivity first
                health_resp = await client.get("/health")
                health_resp.raise_for_status()

                for project in self._project_names:
                    record = await self._collect_project(client, project)
                    if record is not None:
                        records.append(record)
        except httpx.HTTPError as exc:
            log.warning(
                "delightd collect failed",
                extra={"url": self._base_url, "error": str(exc)},
            )
        return records

    async def _collect_project(
        self,
        client: httpx.AsyncClient,
        project: str,
    ) -> ServiceRecord | None:
        try:
            resp = await client.get(f"/projects/{project}/state")
            resp.raise_for_status()
            data = resp.json()

            state_raw = data.get("state", "unknown")
            status = _STATE_MAP.get(state_raw, "unknown")

            return ServiceRecord(
                name=project,
                source="delightd",
                status=status,
                metadata={
                    "backup_state": state_raw,
                    "error_count": data.get("error_count", 0),
                    "last_activity": data.get("last_activity"),
                    "next_retry": data.get("next_retry"),
                },
            )
        except httpx.HTTPError as exc:
            log.warning(
                "delightd project state fetch failed",
                extra={"project": project, "error": str(exc)},
            )
            return None

    async def healthy(self) -> SourceHealth:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            ) as client:
                resp = await client.get("/health")
                resp.raise_for_status()
                latency = (time.monotonic() - t0) * 1000
                return SourceHealth(
                    name=self.name,
                    reachable=True,
                    latency_ms=round(latency, 2),
                )
        except Exception as exc:
            latency = (time.monotonic() - t0) * 1000
            return SourceHealth(
                name=self.name,
                reachable=False,
                latency_ms=round(latency, 2),
                error=str(exc),
            )

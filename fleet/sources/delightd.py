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
        base_url: str | None = None,
        project_names: list[str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url
        self._project_names = project_names or []
        self._timeout = timeout

    async def _resolve_host(self) -> tuple[str, str | None]:
        if self._base_url:
            return self._base_url.rstrip("/"), None
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get("http://127.0.0.1:8080/api/http/routers")
                if resp.status_code == 200:
                    routers = resp.json()
                    for r in routers:
                        if r.get("name", "").startswith("delightd"):
                            rule = r.get("rule", "")
                            m = re.search(r"Host\(`([^`]+)`\)", rule)
                            if m:
                                hostname = m.group(1)
                                return "http://127.0.0.1:80", hostname
        except Exception as e:
            log.debug(f"Failed to resolve delightd hostname from Traefik: {e}")
        return "http://127.0.0.1:8088", None

    async def collect(self) -> list[ServiceRecord]:
        records: list[ServiceRecord] = []
        try:
            url, host_header = await self._resolve_host()
            headers = {"Host": host_header} if host_header else {}
            async with httpx.AsyncClient(
                base_url=url,
                timeout=self._timeout,
                headers=headers,
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
                extra={"url": getattr(self, "_base_url", "unknown"), "error": str(exc)},
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
            url, host_header = await self._resolve_host()
            headers = {"Host": host_header} if host_header else {}
            async with httpx.AsyncClient(
                base_url=url,
                timeout=self._timeout,
                headers=headers,
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

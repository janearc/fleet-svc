from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fleet.models import ServiceRecord, SourceHealth
from fleet.sources.base import Source

log = logging.getLogger(__name__)

ESSENTIAL_LABEL = "fleet.essential"

# colima's default per-profile docker socket on macos. docker.from_env() only
# consults the DOCKER_HOST env var; it does NOT read the active docker CLI
# context, so on a colima host with DOCKER_HOST unset it falls back to
# /var/run/docker.sock (which colima does not provide) and the client comes up
# dead. we resolve the endpoint ourselves: DOCKER_HOST -> active docker context
# -> the colima default socket -> from_env().
_COLIMA_DEFAULT_SOCK = Path.home() / ".colima" / "default" / "docker.sock"


def _resolve_base_url() -> str | None:
    # honour an explicit DOCKER_HOST first — the operator's override wins.
    host = os.environ.get("DOCKER_HOST")
    if host:
        return host

    # otherwise follow the active docker CLI context (e.g. colima), matching
    # what `docker` on the command line would talk to.
    try:
        from docker.context import ContextAPI

        ctx = ContextAPI.get_current_context()
        endpoint = ctx.endpoints.get("docker", {}) if ctx else {}
        host = endpoint.get("Host")
        if host:
            return host
    except Exception as exc:
        # the docker lib may be absent or the context store unreadable; fall
        # through to the colima default below.
        log.debug("could not resolve docker context: %s", exc)

    # last resort before from_env(): the colima default socket, if present.
    if _COLIMA_DEFAULT_SOCK.exists():
        return f"unix://{_COLIMA_DEFAULT_SOCK}"

    return None

# docker container status -> fleet normalized status
_STATUS_MAP: dict[str, str] = {
    "running": "running",
    "exited": "stopped",
    "paused": "paused",
    "restarting": "running",
    "removing": "stopped",
    "dead": "error",
    "created": "stopped",
}


def _parse_ports(ports_raw: dict) -> list[str]:
    # container.ports: {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}
    result: list[str] = []
    if not ports_raw:
        return result
    for container_port, bindings in ports_raw.items():
        if not bindings:
            result.append(container_port)
            continue
        for binding in bindings:
            host_ip = binding.get("HostIp", "0.0.0.0")
            host_port = binding.get("HostPort", "?")
            result.append(f"{host_ip}:{host_port}->{container_port}")
    return result


def _compute_uptime(started_at: str | None) -> str | None:
    if not started_at or started_at.startswith("0001"):
        return None
    try:
        # docker returns RFC3339 with possible nanosecond precision
        # truncate fractional seconds to 6 digits for stdlib
        clean = started_at
        if "." in clean:
            before_dot, after_dot = clean.split(".", 1)
            frac = ""
            tz_suffix = ""
            for i, ch in enumerate(after_dot):
                if ch in "+-Z":
                    tz_suffix = after_dot[i:]
                    break
                frac += ch
            clean = f"{before_dot}.{frac[:6]}{tz_suffix}"

        started = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - started
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return None

        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)

        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)
    except (ValueError, TypeError):
        return None


def _extract_image(container: Any) -> str | None:
    try:
        img = container.image
        if img and img.tags:
            return img.tags[0]
        if img:
            return img.short_id
    except Exception:
        pass
    return None


class DockerSource(Source):
    name = "docker"

    def __init__(self, client: Any = None) -> None:
        # lazy-import docker to avoid hard dep at import time
        if client is not None:
            self._client = client
        else:
            try:
                import docker

                base_url = _resolve_base_url()
                if base_url:
                    self._client = docker.DockerClient(base_url=base_url)
                else:
                    # no DOCKER_HOST, no context, no colima socket: let the lib
                    # try its own defaults rather than guessing.
                    self._client = docker.from_env()
            except Exception as exc:
                # graceful degradation: a missing socket / unavailable daemon
                # must not crash fleet — the source simply reports unreachable.
                log.warning("could not init docker client: %s", exc)
                self._client = None

    async def collect(self) -> list[ServiceRecord]:
        if self._client is None:
            return []
        try:
            containers = self._client.containers.list(all=True)
        except Exception as exc:
            log.warning("docker collect failed", extra={"error": str(exc)})
            return []

        records: list[ServiceRecord] = []
        for c in containers:
            labels = c.labels or {}
            state_attrs = c.attrs.get("State", {})
            raw_status = (c.status or "unknown").lower()
            status = _STATUS_MAP.get(raw_status, "unknown")

            uptime = (
                _compute_uptime(state_attrs.get("StartedAt"))
                if status == "running"
                else None
            )

            # strip leading / from container name
            name = c.name or ""
            if name.startswith("/"):
                name = name[1:]

            # Compute deployment
            deployment = labels.get("com.docker.compose.project")
            
            image_name = None
            if hasattr(c, "image") and c.image and hasattr(c.image, "tags") and c.image.tags:
                image_name = c.image.tags[0]
            
            if not deployment and image_name:
                deployment = image_name.split(":")[0].split("/")[-1]
            elif not deployment:
                deployment = "docker-standalone"

            records.append(
                ServiceRecord(
                    name=name,
                    source="docker",
                    status=status,
                    deployment=deployment,
                    essential=labels.get(ESSENTIAL_LABEL, "").lower() in ("true", "1", "yes"),
                    paused_by_fleet=labels.get("fleet.paused", "").lower() in ("true", "1", "yes"),
                    prev_state=labels.get("fleet.prev-state"),
                    image=_extract_image(c),
                    ports=_parse_ports(c.ports),
                    uptime=uptime,
                    metadata={"container_id": c.short_id},
                )
            )
        return records

    async def healthy(self) -> SourceHealth:
        if self._client is None:
            return SourceHealth(name=self.name, reachable=False, error="docker client unavailable")
        t0 = time.monotonic()
        try:
            self._client.ping()
            latency = (time.monotonic() - t0) * 1000
            return SourceHealth(name=self.name, reachable=True, latency_ms=round(latency, 2))
        except Exception as exc:
            latency = (time.monotonic() - t0) * 1000
            return SourceHealth(name=self.name, reachable=False, latency_ms=round(latency, 2), error=str(exc))

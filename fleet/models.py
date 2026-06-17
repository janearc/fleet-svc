from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


SourceName = str

ServiceStatus = Literal[
    "running",
    "stopped",
    "paused",
    "error",
    "routed",
    "unknown",
]


class ServiceRecord(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    source: SourceName
    status: ServiceStatus
    essential: bool = False
    paused_by_fleet: bool = False
    image: str | None = None
    ports: list[str] = Field(default_factory=list)
    uptime: str | None = None
    replicas: int | None = None
    prev_replicas: int | None = None
    prev_state: str | None = None
    namespace: str | None = None
    deployment: str | None = None
    # populated by prometheus scraper
    diagnostics: dict = Field(default_factory=dict)
    # arbitrary source-specific data
    metadata: dict = Field(default_factory=dict)


class SourceHealth(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    reachable: bool
    latency_ms: float | None = None
    error: str | None = None


class FleetState(BaseModel):
    services: list[ServiceRecord]
    sources: list[SourceHealth]
    collected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class PauseResult(BaseModel):
    action: Literal["pause", "resume"]
    dry_run: bool
    affected: list[ServiceRecord]
    skipped: list[ServiceRecord]
    errors: list[dict]

# Which container engine fleet should drive on this workstation.
#   auto           - prefer colima, fall back to Docker Desktop (legacy sniff)
#   colima         - require colima; never silently start anything else
#   docker-desktop - require Docker Desktop; never silently start colima
# "auto" preserves prior behaviour. The explicit values are a contract: if the
# named runtime is absent, bootstrap fails loudly rather than starting a runtime
# the operator did not ask for (e.g. silently launching Docker Desktop on a host
# whose owner has deliberately retired it).
DockerRuntime = Literal["auto", "colima", "docker-desktop"]


class WorkstationHost(BaseModel):
    os: str
    arch: str
    daemons: list[str]
    docker_runtime: DockerRuntime = "auto"
    packages: list[str] = Field(default_factory=list)
    casks: list[str] = Field(default_factory=list)

class WorkstationRepo(BaseModel):
    name: str
    origin: str
    path: str
    essential: bool = False

class WorkstationModel(BaseModel):
    provider: str
    id: str
    file: str | None = None

class WorkstationConfig(BaseModel):
    version: str
    host: WorkstationHost
    repositories: list[WorkstationRepo]
    models: list[WorkstationModel] = Field(default_factory=list)

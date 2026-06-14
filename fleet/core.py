import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fleet.actions import PauseManager
from fleet.diagnostics import DiagnosticsCollector
from fleet.models import FleetState, PauseResult, ServiceRecord, SourceHealth
from fleet.state import PauseJournal
from fleet.sources import DockerSource, KubeSource, DelightdSource
from fleet.sources.traefik import TraefikSource
from fleet.sources.envoy import EnvoySource
from fleet.sources.transparent import TransparentSource

log = logging.getLogger(__name__)

class FleetCore:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self.sources = []
        self._journal = None
        self._diagnostics = None
        self._pause_manager = None
        self._init_journal()
        self._init_diagnostics()
        self._init_sources()

    def _init_sources(self) -> None:
        try:
            self.sources.append(DockerSource())
        except Exception as e:
            log.warning("Could not init DockerSource: %s", e)
            
        try:
            self.sources.append(KubeSource())
        except Exception as e:
            log.warning("Could not init KubeSource: %s", e)
            
        try:
            self.sources.append(DelightdSource())
        except Exception as e:
            log.warning("Could not init DelightdSource: %s", e)
            
        try:
            self.sources.append(TraefikSource())
        except Exception as e:
            log.warning("Could not init TraefikSource: %s", e)
            
        try:
            self.sources.append(EnvoySource())
        except Exception as e:
            log.warning("Could not init EnvoySource: %s", e)
            
        try:
            self.sources.append(TransparentSource())
        except Exception as e:
            log.warning("Could not init TransparentSource: %s", e)

    def _init_journal(self) -> None:
        db_path = self._config.get("journal_db_path")
        self._journal = PauseJournal(db_path=db_path)
        self._pause_manager = PauseManager(self._journal)

    def _init_diagnostics(self) -> None:
        timeout = self._config.get("diagnostics_timeout", 5.0)
        self._diagnostics = DiagnosticsCollector(timeout=timeout)

    async def show(self, source_filter: str | None = None) -> FleetState:
        sources_to_query = [s for s in self.sources if not source_filter or s.name == source_filter]
        
        by_source: dict[str, list[ServiceRecord]] = {}
        source_health: list[SourceHealth] = []

        tasks = {s.name: asyncio.create_task(s.collect()) for s in sources_to_query}
        health_tasks = {s.name: asyncio.create_task(s.healthy()) for s in sources_to_query}
        
        if tasks:
            await asyncio.wait(list(tasks.values()))
            await asyncio.wait(list(health_tasks.values()))

        for s in sources_to_query:
            try:
                health = health_tasks[s.name].result()
                source_health.append(health)
            except Exception as e:
                source_health.append(SourceHealth(name=s.name, reachable=False, error=str(e)))
                
            try:
                services = tasks[s.name].result()
                by_source[s.name] = services
            except Exception as e:
                by_source[s.name] = []

        merged = self._merge_services(by_source)

        paused_set = {
            row["service_name"]
            for row in (self._journal.get_paused_services() if self._journal else [])
        }
        for svc in merged:
            if svc.name in paused_set:
                svc.paused_by_fleet = True

        return FleetState(
            services=merged,
            sources=source_health,
            collected_at=datetime.now(timezone.utc),
        )

    async def pause(self, dry_run: bool = False) -> PauseResult:
        state = await self.show()
        pausable = [
            svc for svc in state.services
            if svc.status == "running" and not svc.paused_by_fleet
        ]
        return await self._pause_manager.execute_pause(pausable, dry_run=dry_run)

    async def resume(self, dry_run: bool = False) -> PauseResult:
        return await self._pause_manager.execute_resume(dry_run=dry_run)

    async def selfcheck(self) -> list[SourceHealth]:
        healths: list[SourceHealth] = []
        tasks = {s.name: asyncio.create_task(s.healthy()) for s in self.sources}
        if tasks:
            await asyncio.wait(list(tasks.values()))
            
        for s in self.sources:
            try:
                healths.append(tasks[s.name].result())
            except Exception as e:
                healths.append(SourceHealth(name=s.name, reachable=False, error=str(e)))
        return healths

    def _merge_services(self, by_source: dict[str, list[ServiceRecord]]) -> list[ServiceRecord]:
        grouped: dict[str, list[ServiceRecord]] = defaultdict(list)
        for source, records in by_source.items():
            for rec in records:
                grouped[rec.name].append(rec)

        merged: list[ServiceRecord] = []
        for svc_name, records in grouped.items():
            # For simplicity, pick the first record as base, then merge metadata
            base = records[0].model_copy()
            for r in records[1:]:
                base.metadata.update(r.metadata)
                base.ports.extend(r.ports)
                # Lifecycle state precedence over routing precedence etc can be added here
                if r.status in ("running", "paused", "stopped"):
                    base.status = r.status
            
            # Deduplicate ports
            base.ports = list(set(base.ports))
            merged.append(base)

        return merged

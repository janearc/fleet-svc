from __future__ import annotations

import logging
from typing import Any

from fleet.models import ServiceRecord, SourceHealth
from fleet.sources.base import Source

log = logging.getLogger(__name__)


class KubeSource(Source):
    name = "kube"

    def __init__(self, apps_v1: Any = None, core_v1: Any = None, namespace: str = "default"):
        self._apps_v1 = apps_v1
        self._core_v1 = core_v1
        self._namespace = namespace

    async def collect(self) -> list[ServiceRecord]:
        if self._apps_v1 is None:
            return []
        try:
            deploys = self._apps_v1.list_namespaced_deployment(self._namespace)
        except Exception as exc:
            log.warning("kube collect failed: %s", exc)
            return []

        records: list[ServiceRecord] = []
        for d in deploys.items:
            meta = d.metadata
            spec = d.spec
            status_obj = d.status
            annotations = meta.annotations or {}
            labels = meta.labels or {}

            ready = getattr(status_obj, "ready_replicas", None) or 0
            desired = spec.replicas or 0

            if desired == 0:
                status = "stopped"
            elif ready >= desired:
                status = "running"
            elif ready > 0:
                status = "error"
            else:
                status = "stopped"

            # extract image from first container
            image = None
            if spec.template and spec.template.spec and spec.template.spec.containers:
                image = spec.template.spec.containers[0].image

            records.append(
                ServiceRecord(
                    name=meta.name,
                    source="kube",
                    status=status,
                    essential=labels.get("fleet.essential", "").lower() in ("true", "1", "yes"),
                    paused_by_fleet=annotations.get("fleet.paused", "").lower() in ("true", "1", "yes"),
                    image=image,
                    replicas=ready,
                    prev_replicas=int(annotations["fleet.prev-replicas"]) if "fleet.prev-replicas" in annotations else None,
                    namespace=meta.namespace,
                    metadata={"uid": meta.uid},
                )
            )
        return records

    async def healthy(self) -> SourceHealth:
        if self._apps_v1 is None:
            return SourceHealth(name=self.name, reachable=False, error="kube client unavailable")
        try:
            import time
            t0 = time.monotonic()
            self._apps_v1.list_namespaced_deployment(self._namespace, limit=1)
            latency = (time.monotonic() - t0) * 1000
            return SourceHealth(name=self.name, reachable=True, latency_ms=round(latency, 2))
        except Exception as exc:
            return SourceHealth(name=self.name, reachable=False, error=str(exc))

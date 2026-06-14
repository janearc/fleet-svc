from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fleet.models import ServiceRecord

log = logging.getLogger(__name__)

# Thresholds
_MEMORY_HIGH_BYTES = 1_073_741_824  # 1 GiB
_ERROR_RATE_THRESHOLD = 0.01  # 1% 5xx

# Regex for prometheus text exposition format
# Matches: metric_name{labels} value
# and:     metric_name value
_METRIC_LINE_RE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)'
    r'(?:\{(?P<labels>[^}]*)\})?\s+'
    r'(?P<value>[^\s]+)'
)


class DiagnosticsCollector:
    def __init__(self, timeout: float = 5.0) -> None:
        self._timeout = timeout

    @staticmethod
    def parse_prometheus_text(text: str) -> dict[str, float]:
        # Parse prometheus exposition format into a flat dict.
        # For metrics with labels, the key includes the label set,
        # e.g. 'http_requests_total{status="500"}' -> 42.0
        # For metrics without labels, just the name.
        metrics: dict[str, float] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = _METRIC_LINE_RE.match(line)
            if match is None:
                continue
            name = match.group("name")
            labels = match.group("labels")
            value_str = match.group("value")
            try:
                value = float(value_str)
            except ValueError:
                continue

            if labels:
                key = f'{name}{{{labels}}}'
            else:
                key = name
            metrics[key] = value

        return metrics

    async def scrape_metrics(self, url: str) -> dict[str, float]:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required for metrics scraping") from exc

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return self.parse_prometheus_text(resp.text)

    async def diagnose_service(self, service: ServiceRecord) -> dict:
        metrics_port = service.metadata.get("metrics_port")
        metrics_path = service.metadata.get("metrics_path", "/metrics")
        metrics_host = service.metadata.get("metrics_host", "127.0.0.1")

        if metrics_port is None:
            return {}

        url = f"http://{metrics_host}:{metrics_port}{metrics_path}"
        try:
            metrics = await self.scrape_metrics(url)
        except Exception as exc:
            log.warning("failed to scrape %s: %s", url, exc)
            return {"scrape_error": str(exc)}

        findings: dict = {}

        # Check memory pressure
        mem = metrics.get("process_resident_memory_bytes")
        if mem is not None and mem > _MEMORY_HIGH_BYTES:
            findings["memory_high"] = True
            findings["memory_bytes"] = mem

        # Check if service is down
        up = metrics.get("up")
        if up is not None and up == 0:
            findings["down"] = True

        # Check 5xx error rate
        # Collect all http_requests_total entries, compute 5xx ratio
        total_requests = 0.0
        error_requests = 0.0
        for key, val in metrics.items():
            if not key.startswith("http_requests_total{"):
                continue
            total_requests += val
            # Check for 5xx status codes in labels
            if re.search(r'status="5\d\d"', key):
                error_requests += val

        if total_requests > 0:
            error_rate = error_requests / total_requests
            findings["error_rate"] = round(error_rate, 6)
            if error_rate > _ERROR_RATE_THRESHOLD:
                findings["error_rate_high"] = True

        return findings

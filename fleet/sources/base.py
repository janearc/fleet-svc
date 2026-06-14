from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from fleet.models import ServiceRecord, SourceHealth

log = logging.getLogger(__name__)


class Source(ABC):
    name: str

    @abstractmethod
    async def collect(self) -> list[ServiceRecord]:
        ...

    @abstractmethod
    async def healthy(self) -> SourceHealth:
        ...

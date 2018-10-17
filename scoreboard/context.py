"""Wiring of the long-lived application objects."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from scoreboard.broker import Broker, InMemoryBroker
from scoreboard.config import Settings
from scoreboard.hub import Hub
from scoreboard.repository import InMemoryMatchRepository, MatchRepository
from scoreboard.service import ScoreboardService

if TYPE_CHECKING:
    from scoreboard.web.socket import ScoreboardSocket


@dataclass
class AppContext:
    settings: Settings
    repository: MatchRepository
    broker: Broker
    service: ScoreboardService
    hub: Hub
    connections: set[ScoreboardSocket] = field(default_factory=set)
    started_at: float = field(default_factory=time.time)
    evicted_slow: int = 0
    evicted_idle: int = 0

    @classmethod
    def build(cls, settings: Settings) -> AppContext:
        broker: Broker
        repository: MatchRepository
        if settings.redis_url:
            from scoreboard.broker.redis_broker import RedisBroker
            from scoreboard.repository.redis_repo import RedisMatchRepository

            broker = RedisBroker.from_url(settings.redis_url)
            repository = RedisMatchRepository.from_url(settings.redis_url, settings.redis_prefix)
        else:
            broker = InMemoryBroker()
            repository = InMemoryMatchRepository()
        return cls.from_parts(settings, repository, broker)

    @classmethod
    def from_parts(
        cls, settings: Settings, repository: MatchRepository, broker: Broker
    ) -> AppContext:
        return cls(
            settings=settings,
            repository=repository,
            broker=broker,
            service=ScoreboardService(repository, broker),
            hub=Hub(broker, repository),
        )

    async def close(self) -> None:
        for conn in list(self.connections):
            conn.close(code=1001, reason="server shutting down")
        await self.hub.close()
        await self.broker.close()
        await self.repository.close()

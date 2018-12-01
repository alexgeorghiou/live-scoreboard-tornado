"""Smoke tests against a real Redis server (run in CI via a service container)."""

import asyncio
import os
import uuid

import pytest

from scoreboard.broker.redis_broker import RedisBroker
from scoreboard.domain.events import parse_event
from scoreboard.repository.redis_repo import RedisMatchRepository
from tests.factories import new_state

pytestmark = pytest.mark.redis


@pytest.fixture
async def prefix():
    value = f"test-{uuid.uuid4().hex[:8]}"
    yield value
    repo = RedisMatchRepository.from_url(os.environ["REDIS_URL"], value)
    keys = [k async for k in repo._r.scan_iter(f"{value}:*")]
    if keys:
        await repo._r.delete(*keys)
    await repo.close()


async def test_repository_round_trip(prefix: str) -> None:
    repo = RedisMatchRepository.from_url(os.environ["REDIS_URL"], prefix)
    try:
        await repo.create(new_state())
        await repo.append("m1", parse_event({"type": "period_change", "period": "first_half"}))
        goals = [
            parse_event({"type": "goal", "team": "away", "player": "Ito", "minute": i})
            for i in range(1, 9)
        ]
        await asyncio.gather(*(repo.append("m1", g) for g in goals))
        state = await repo.get("m1")
        assert state.score.away == 8
        assert state.version == 9
        assert [e.seq for e in await repo.events("m1")] == list(range(1, 10))
    finally:
        await repo.close()


async def test_broker_fan_out(prefix: str) -> None:
    sub = RedisBroker.from_url(os.environ["REDIS_URL"], poll_timeout=0.1)
    pub = RedisBroker.from_url(os.environ["REDIS_URL"], poll_timeout=0.1)
    seen: list[str] = []
    try:
        await sub.subscribe(f"{prefix}:chan", lambda _c, m: seen.append(m))
        for _ in range(50):
            await pub.publish(f"{prefix}:chan", "hello")
            if seen:
                break
            await asyncio.sleep(0.05)
        assert seen and seen[0] == "hello"
        assert await sub.ping()
    finally:
        await sub.close()
        await pub.close()

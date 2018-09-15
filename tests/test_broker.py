import asyncio

import fakeredis
import pytest

from scoreboard.broker import InMemoryBroker, match_channel
from scoreboard.broker.redis_broker import RedisBroker


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def test_match_channel_name() -> None:
    assert match_channel("abc") == "scoreboard:match:abc"


async def test_in_memory_delivers_in_order_to_all_subscribers() -> None:
    broker = InMemoryBroker()
    a: list[str] = []
    b: list[str] = []
    await broker.subscribe("c", lambda _ch, m: a.append(m))

    async def async_handler(_ch: str, m: str) -> None:
        b.append(m)

    await broker.subscribe("c", async_handler)
    for i in range(3):
        await broker.publish("c", str(i))
    await broker.publish("other", "ignored")
    assert a == b == ["0", "1", "2"]


async def test_in_memory_unsubscribe_and_channel_refcount() -> None:
    broker = InMemoryBroker()
    seen: list[str] = []
    s1 = await broker.subscribe("c", lambda _c, m: seen.append(m))
    s2 = await broker.subscribe("c", lambda _c, m: seen.append(m))
    await s1.cancel()
    await s1.cancel()  # idempotent
    assert broker.channels == {"c"}
    await broker.publish("c", "x")
    await s2.cancel()
    assert broker.channels == set()
    await broker.publish("c", "y")
    assert seen == ["x"]


async def test_failing_handler_does_not_block_others() -> None:
    broker = InMemoryBroker()
    seen: list[str] = []

    def boom(_c: str, _m: str) -> None:
        raise RuntimeError("boom")

    await broker.subscribe("c", boom)
    await broker.subscribe("c", lambda _c, m: seen.append(m))
    await broker.publish("c", "ok")
    assert seen == ["ok"]


@pytest.fixture
async def redis_broker():
    server = fakeredis.FakeServer()
    broker = RedisBroker(
        fakeredis.FakeAsyncRedis(server=server, decode_responses=True), poll_timeout=0.05
    )
    publisher = RedisBroker(
        fakeredis.FakeAsyncRedis(server=server, decode_responses=True), poll_timeout=0.05
    )
    yield broker, publisher
    await broker.close()
    await publisher.close()


async def test_redis_broker_fans_out_between_instances(redis_broker) -> None:
    subscriber, publisher = redis_broker
    seen: list[tuple[str, str]] = []
    await subscriber.subscribe("scoreboard:match:1", lambda c, m: seen.append((c, m)))
    await publisher.publish("scoreboard:match:1", "hello")
    await publisher.publish("scoreboard:match:2", "not for us")
    await _wait_for(lambda: len(seen) == 1)
    await asyncio.sleep(0.1)
    assert seen == [("scoreboard:match:1", "hello")]
    assert await subscriber.ping()


async def test_redis_broker_unsubscribes_upstream(redis_broker) -> None:
    subscriber, publisher = redis_broker
    seen: list[str] = []
    sub = await subscriber.subscribe("c", lambda _c, m: seen.append(m))
    await publisher.publish("c", "1")
    await _wait_for(lambda: seen == ["1"])
    await sub.cancel()
    await publisher.publish("c", "2")
    await asyncio.sleep(0.15)
    assert seen == ["1"]

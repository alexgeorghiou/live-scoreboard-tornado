import json

import pytest

from scoreboard.broker import InMemoryBroker, match_channel
from scoreboard.hub import Hub
from scoreboard.repository import InMemoryMatchRepository, MatchNotFoundError
from scoreboard.service import ScoreboardService


class FakeSubscriber:
    def __init__(self, client_id: str, accept: bool = True) -> None:
        self.client_id = client_id
        self.accept = accept
        self.received: list[tuple[str, int]] = []
        self.evicted: str | None = None

    def deliver(self, match_id: str, version: int, payload: str) -> bool:
        if not self.accept:
            return False
        self.received.append((match_id, version))
        return True

    def evict(self, reason: str) -> None:
        self.evicted = reason


@pytest.fixture
async def setup():
    broker = InMemoryBroker()
    repo = InMemoryMatchRepository()
    service = ScoreboardService(repo, broker)
    hub = Hub(broker, repo)
    for mid in ("a", "b"):
        await service.create_match({"id": mid, "home_team": "H", "away_team": "A"})
        await service.post_event(mid, {"type": "period_change", "period": "first_half"})
    return broker, service, hub


def goal() -> dict:
    return {"type": "goal", "team": "home", "player": "P", "minute": 3}


async def test_fan_out_only_to_subscribers_of_match(setup) -> None:
    broker, service, hub = setup
    alice, bob = FakeSubscriber("alice"), FakeSubscriber("bob")
    state = await hub.subscribe(alice, "a")
    assert state.version == 1
    await hub.subscribe(bob, "b")
    await service.post_event("a", goal())
    assert alice.received == [("a", 2)]
    assert bob.received == []


async def test_one_broker_subscription_per_match(setup) -> None:
    broker, _service, hub = setup
    subs = [FakeSubscriber(str(i)) for i in range(5)]
    for s in subs:
        await hub.subscribe(s, "a")
    assert broker.channels == {match_channel("a")}
    assert hub.watchers("a") == 5
    for s in subs[:-1]:
        await hub.unsubscribe(s, "a")
    assert broker.channels == {match_channel("a")}
    await hub.unsubscribe(subs[-1], "a")
    assert broker.channels == set()
    assert hub.matches == []


async def test_remove_drops_all_subscriptions(setup) -> None:
    broker, _service, hub = setup
    s = FakeSubscriber("s")
    await hub.subscribe(s, "a")
    await hub.subscribe(s, "b")
    await hub.remove(s)
    assert broker.channels == set()


async def test_unknown_match_is_rejected(setup) -> None:
    _broker, _service, hub = setup
    with pytest.raises(MatchNotFoundError):
        await hub.subscribe(FakeSubscriber("x"), "nope")
    assert hub.matches == []


async def test_slow_subscriber_is_evicted_without_affecting_others(setup) -> None:
    _broker, service, hub = setup
    fast, slow = FakeSubscriber("fast"), FakeSubscriber("slow", accept=False)
    await hub.subscribe(fast, "a")
    await hub.subscribe(slow, "a")
    await service.post_event("a", goal())
    await service.post_event("a", goal())
    assert slow.evicted == "slow consumer"
    assert fast.received == [("a", 2), ("a", 3)]
    assert hub.watchers("a") == 1
    assert hub.stats.evictions == 1


async def test_malformed_broker_messages_are_ignored(setup) -> None:
    broker, _service, hub = setup
    s = FakeSubscriber("s")
    await hub.subscribe(s, "a")
    await broker.publish(match_channel("a"), "not json")
    await broker.publish(match_channel("a"), json.dumps({"no": "id"}))
    assert s.received == []
    assert hub.stats.malformed == 2


async def test_close_releases_broker(setup) -> None:
    broker, _service, hub = setup
    await hub.subscribe(FakeSubscriber("s"), "a")
    await hub.close()
    assert broker.channels == set()

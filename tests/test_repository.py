"""Contract tests run against every repository backend."""

import asyncio

import fakeredis
import pytest

from scoreboard.domain.events import parse_event
from scoreboard.domain.state import IllegalEventError, MatchState, Score, derive_state
from scoreboard.repository import (
    InMemoryMatchRepository,
    MatchExistsError,
    MatchNotFoundError,
    VersionConflictError,
)
from scoreboard.repository.redis_repo import RedisMatchRepository
from tests.factories import new_state


@pytest.fixture(params=["memory", "fakeredis"])
async def repo(request):
    if request.param == "memory":
        yield InMemoryMatchRepository()
    else:
        r = RedisMatchRepository(fakeredis.FakeAsyncRedis(decode_responses=True), prefix="t")
        yield r
        await r.close()


def kick(**extra):
    return parse_event({"type": "period_change", "period": "first_half", **extra})


def goal(team="home", **extra):
    return parse_event({"type": "goal", "team": team, "player": "Silva", "minute": 5, **extra})


async def test_create_get_and_list(repo) -> None:
    await repo.create(new_state("a"))
    await repo.create(new_state("b"))
    assert (await repo.get("a")).home_team == "Rovers"
    assert sorted(s.match_id for s in await repo.list()) == ["a", "b"]
    with pytest.raises(MatchExistsError):
        await repo.create(new_state("a"))
    with pytest.raises(MatchNotFoundError):
        await repo.get("zzz")


async def test_append_assigns_sequence_numbers(repo) -> None:
    await repo.create(new_state())
    first = await repo.append("m1", kick())
    second = await repo.append("m1", goal())
    assert (first.event.seq, second.event.seq) == (1, 2)
    assert second.state.score == Score(1, 0)
    assert [e.seq for e in await repo.events("m1")] == [1, 2]
    assert [e.seq for e in await repo.events("m1", since=1)] == [2]


async def test_duplicate_event_id_is_idempotent(repo) -> None:
    await repo.create(new_state())
    await repo.append("m1", kick())
    g = goal(id="feed-1")
    await repo.append("m1", g)
    again = await repo.append("m1", g)
    assert again.duplicate
    assert again.state.score.home == 1
    assert again.event.seq == 2
    assert len(await repo.events("m1")) == 2


async def test_illegal_event_leaves_state_untouched(repo) -> None:
    await repo.create(new_state())
    with pytest.raises(IllegalEventError):
        await repo.append("m1", goal())
    assert (await repo.get("m1")).version == 0
    assert await repo.events("m1") == []


async def test_expected_version_conflict(repo) -> None:
    await repo.create(new_state())
    await repo.append("m1", kick(), expected_version=0)
    with pytest.raises(VersionConflictError) as info:
        await repo.append("m1", goal(), expected_version=0)
    assert info.value.actual == 1


async def test_append_to_unknown_match(repo) -> None:
    with pytest.raises(MatchNotFoundError):
        await repo.append("nope", kick())
    with pytest.raises(MatchNotFoundError):
        await repo.events("nope")


async def test_snapshot_replaces_base_and_bumps_version(repo) -> None:
    await repo.create(new_state())
    await repo.append("m1", kick())
    snapshot = MatchState.from_dict(
        {**new_state().to_dict(), "period": "second_half", "score": {"home": 2, "away": 2}}
    )
    installed = await repo.replace(snapshot)
    assert installed.version == 2
    assert await repo.events("m1") == []
    after = await repo.append("m1", goal(team="away"))
    assert after.state.score == Score(2, 3)
    assert derive_state(installed, await repo.events("m1")) == after.state


async def test_concurrent_appends_are_serialised(repo) -> None:
    await repo.create(new_state())
    await repo.append("m1", kick())
    results = await asyncio.gather(*(repo.append("m1", goal(minute=i)) for i in range(10)))
    assert sorted(r.event.seq for r in results) == list(range(2, 12))
    assert (await repo.get("m1")).score.home == 10

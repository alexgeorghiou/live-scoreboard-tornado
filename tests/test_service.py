import json

import pytest

from scoreboard.broker import InMemoryBroker, match_channel
from scoreboard.domain.events import ValidationError
from scoreboard.repository import InMemoryMatchRepository, MatchExistsError
from scoreboard.service import ScoreboardService


@pytest.fixture
def service() -> ScoreboardService:
    return ScoreboardService(InMemoryMatchRepository(), InMemoryBroker())


async def test_create_match_validates_payload(service: ScoreboardService) -> None:
    state = await service.create_match({"id": "derby", "home_team": " A ", "away_team": "B"})
    assert (state.match_id, state.home_team) == ("derby", "A")
    with pytest.raises(MatchExistsError):
        await service.create_match({"id": "derby", "home_team": "A", "away_team": "B"})
    with pytest.raises(ValidationError, match="itself"):
        await service.create_match({"home_team": "A", "away_team": "a"})
    with pytest.raises(ValidationError, match="match id"):
        await service.create_match({"id": "Bad Id!", "home_team": "A", "away_team": "B"})
    with pytest.raises(ValidationError, match="away_team"):
        await service.create_match({"home_team": "A"})


async def test_create_match_generates_id(service: ScoreboardService) -> None:
    state = await service.create_match({"home_team": "A", "away_team": "B"})
    assert len(state.match_id) == 12


async def test_post_event_publishes_state(service: ScoreboardService) -> None:
    await service.create_match({"id": "m", "home_team": "A", "away_team": "B"})
    await service.post_event("m", {"type": "period_change", "period": "first_half"})
    await service.post_event("m", {"id": "g", "type": "goal", "team": "away", "player": "Z"})
    # A retried delivery must not be fanned out twice.
    await service.post_event("m", {"id": "g", "type": "goal", "team": "away", "player": "Z"})
    published = service.broker.published  # type: ignore[attr-defined]
    assert [ch for ch, _ in published] == [match_channel("m")] * 2
    last = json.loads(published[-1][1])
    assert last["type"] == "event"
    assert last["version"] == 2
    assert last["state"]["score"] == {"home": 0, "away": 1}


async def test_snapshot_merges_and_publishes(service: ScoreboardService) -> None:
    await service.create_match({"id": "m", "home_team": "A", "away_team": "B"})
    state = await service.put_snapshot(
        "m", {"period": "second_half", "score": {"home": 3, "away": 1}, "minute": 70}
    )
    assert state.home_team == "A"
    assert state.score.home == 3
    assert state.version == 1
    message = json.loads(service.broker.published[-1][1])  # type: ignore[attr-defined]
    assert message["type"] == "snapshot"
    assert message["reason"] == "resync"


async def test_snapshot_rejects_invalid_state(service: ScoreboardService) -> None:
    await service.create_match({"id": "m", "home_team": "A", "away_team": "B"})
    with pytest.raises(ValidationError):
        await service.put_snapshot("m", {"period": "tea_break"})
    with pytest.raises(ValidationError):
        await service.put_snapshot("m", ["not", "a", "dict"])

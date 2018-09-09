import pytest

from scoreboard.domain.events import (
    CardColour,
    EventType,
    Period,
    Team,
    ValidationError,
    event_from_dict,
    parse_event,
)


def test_parse_goal() -> None:
    event = parse_event({"type": "goal", "team": "home", "player": " Silva ", "minute": 23})
    assert event.type is EventType.GOAL
    assert event.team is Team.HOME
    assert event.player == "Silva"
    assert event.minute == 23
    assert len(event.id) == 32


def test_parse_keeps_client_supplied_id() -> None:
    event = parse_event({"id": "feed-42", "type": "period_change", "period": "first_half"})
    assert event.id == "feed-42"
    assert event.period is Period.FIRST_HALF


def test_parse_card_and_substitution() -> None:
    card = parse_event({"type": "card", "team": "away", "player": "Okafor", "card": "red"})
    assert card.card is CardColour.RED
    sub = parse_event(
        {"type": "substitution", "team": "away", "player_off": "A", "player_on": "B", "minute": 60}
    )
    assert (sub.player_off, sub.player_on) == ("A", "B")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "JSON object"),
        ({"type": "throw_in"}, "'type' must be one of"),
        ({"type": "goal", "team": "home", "player": "X", "minute": "10"}, "integer"),
        ({"type": "goal", "team": "home", "player": "X", "minute": True}, "integer"),
        ({"type": "goal", "team": "home", "player": "X", "minute": 151}, "between"),
        ({"type": "goal", "team": "centre", "player": "X"}, "'team'"),
        ({"type": "goal", "team": "home", "player": ""}, "'player'"),
        ({"type": "goal", "team": "home", "player": "X", "own_goal": True, "penalty": True}, "own"),
        ({"type": "card", "team": "home", "player": "X", "card": "green"}, "'card'"),
        ({"type": "substitution", "team": "home", "player_off": "A", "player_on": "A"}, "differ"),
        ({"type": "goal_cancelled"}, "'ref_event_id'"),
        ({"type": "period_change", "period": "overtime"}, "'period'"),
    ],
)
def test_invalid_payloads(payload: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_event(payload)


def test_round_trip_through_dict_preserves_seq() -> None:
    event = parse_event({"type": "goal", "team": "away", "player": "Ito", "penalty": True})
    restored = event_from_dict(event.with_seq(7).to_dict())
    assert restored == event.with_seq(7)


def test_to_dict_omits_unset_fields() -> None:
    data = parse_event({"type": "period_change", "period": "half_time", "minute": 45}).to_dict()
    assert data["period"] == "half_time"
    assert "player" not in data
    assert "team" not in data


def test_team_opponent() -> None:
    assert Team.HOME.opponent is Team.AWAY
    assert Team.AWAY.opponent is Team.HOME

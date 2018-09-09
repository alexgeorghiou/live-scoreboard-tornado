import pytest

from scoreboard.domain.events import Period, Team
from scoreboard.domain.state import (
    MAX_SUBSTITUTIONS,
    IllegalEventError,
    MatchState,
    apply_event,
    derive_state,
)
from tests.factories import ev, goal, kickoff, new_state, period, play


def test_initial_state_is_scheduled() -> None:
    state = new_state()
    assert state.status == "scheduled"
    assert state.score.home == state.score.away == 0


def test_goals_update_score_and_version() -> None:
    state = play(kickoff(), goal("home", "Silva", 12), goal("away", "Ito", 30))
    state = play(state, goal("home", "Silva", 41))
    assert (state.score.home, state.score.away) == (2, 1)
    assert state.version == 4
    assert state.minute == 41
    assert state.status == "live"


def test_own_goal_credits_named_team() -> None:
    # 'team' is the side that benefits; the scorer plays for the other side.
    state = play(kickoff(), goal("away", "Hart", 5, own_goal=True))
    assert state.score.away == 1
    assert state.goals[0].own_goal


def test_goal_rejected_before_kickoff_and_at_half_time() -> None:
    with pytest.raises(IllegalEventError, match="pre_match"):
        play(new_state(), goal("home", "Silva"))
    state = play(kickoff(), period("half_time", 45))
    assert state.status == "break"
    with pytest.raises(IllegalEventError, match="half_time"):
        play(state, goal("home", "Silva", 46))


def test_cancelled_goal_reverts_score() -> None:
    state = kickoff()
    state = apply_event(state, ev(2, id="g1", **goal("home", "Silva", 20)))
    state = play(state, {"type": "goal_cancelled", "ref_event_id": "g1", "minute": 21})
    assert state.score.home == 0
    assert state.goals[0].cancelled
    with pytest.raises(IllegalEventError, match="already been cancelled"):
        play(state, {"type": "goal_cancelled", "ref_event_id": "g1", "minute": 22})
    with pytest.raises(IllegalEventError, match="no goal"):
        play(state, {"type": "goal_cancelled", "ref_event_id": "nope", "minute": 22})


def test_second_yellow_becomes_red() -> None:
    state = play(
        kickoff(),
        {"type": "card", "team": "home", "player": "Diaz", "card": "yellow", "minute": 10},
        {"type": "card", "team": "home", "player": "Diaz", "card": "yellow", "minute": 50},
    )
    assert "Diaz" in state.sent_off(Team.HOME)
    assert "Diaz" not in state.booked(Team.HOME)
    assert state.cards[-1].second_yellow
    with pytest.raises(IllegalEventError, match="already been sent off"):
        play(state, {"type": "card", "team": "home", "player": "Diaz", "card": "red"})


def test_sent_off_player_cannot_score_or_be_substituted() -> None:
    state = play(
        kickoff(), {"type": "card", "team": "away", "player": "Ito", "card": "red", "minute": 3}
    )
    with pytest.raises(IllegalEventError, match="sent off"):
        play(state, goal("away", "Ito", 4))
    with pytest.raises(IllegalEventError, match="sent off"):
        play(
            state,
            {"type": "substitution", "team": "away", "player_off": "Ito", "player_on": "Bo"},
        )
    # Sending-off is tracked per side: a namesake on the other team is unaffected.
    assert play(state, goal("home", "Ito", 5)).score.home == 1


def test_substitution_rules() -> None:
    sub = {"type": "substitution", "team": "home", "minute": 60}
    state = play(kickoff(), {**sub, "player_off": "A", "player_on": "B"})
    with pytest.raises(IllegalEventError, match="already been substituted"):
        play(state, {**sub, "player_off": "A", "player_on": "C"})
    with pytest.raises(IllegalEventError, match="cannot return"):
        play(state, {**sub, "player_off": "D", "player_on": "A"})
    with pytest.raises(IllegalEventError, match="already on the pitch"):
        play(state, {**sub, "player_off": "D", "player_on": "B"})
    with pytest.raises(IllegalEventError, match="substituted off"):
        play(state, goal("home", "A", 61))


def test_substitution_limit_extends_in_extra_time() -> None:
    sub = {"type": "substitution", "team": "away", "minute": 70}
    state = kickoff()
    for i in range(MAX_SUBSTITUTIONS):
        state = play(state, {**sub, "player_off": f"off{i}", "player_on": f"on{i}"})
    with pytest.raises(IllegalEventError, match="no substitutions left"):
        play(state, {**sub, "player_off": "x", "player_on": "y"})
    state = play(
        state,
        period("half_time"),
        period("second_half"),
        period("extra_time_break"),
    )
    state = play(state, {**sub, "player_off": "x", "player_on": "y"})
    assert state.substitutions_used(Team.AWAY) == MAX_SUBSTITUTIONS + 1


def test_full_period_flow_to_penalties() -> None:
    state = play(
        kickoff(),
        period("half_time", 45),
        period("second_half", 46),
        period("extra_time_break", 90),
        period("extra_time_first", 91),
        period("extra_time_half_time", 105),
        period("extra_time_second", 106),
    )
    with pytest.raises(IllegalEventError, match="penalties"):
        play(state, period("full_time", 120))
    state = play(state, period("penalties", 120), period("full_time", 120))
    assert state.status == "finished"
    with pytest.raises(IllegalEventError, match="finished"):
        play(state, goal("home", "Late", 120))


def test_extra_time_requires_level_score() -> None:
    state = play(
        kickoff(), goal("home", "Silva"), period("half_time"), period("second_half")
    )
    with pytest.raises(IllegalEventError, match="level"):
        play(state, period("extra_time_break"))
    assert play(state, period("full_time", 90)).period is Period.FULL_TIME


def test_illegal_period_transition() -> None:
    with pytest.raises(IllegalEventError, match="cannot move"):
        play(new_state(), period("second_half"))


def test_derive_state_matches_incremental_application() -> None:
    events = [
        ev(1, **period("first_half")),
        ev(2, **goal("home", "Silva", 9)),
        ev(3, type="card", team="away", player="Ito", card="yellow", minute=33),
        ev(4, **period("half_time", 45)),
    ]
    derived = derive_state(new_state(), events)
    assert derived.score.home == 1
    assert derived.version == 4
    assert derived.booked(Team.AWAY) == {"Ito"}


def test_state_serialisation_round_trip() -> None:
    state = play(
        kickoff(),
        goal("home", "Silva", 9, penalty=True),
        {"type": "card", "team": "away", "player": "Ito", "card": "yellow", "minute": 33},
        {"type": "substitution", "team": "away", "player_off": "Ito", "player_on": "Bo"},
    )
    data = state.to_dict()
    assert data["status"] == "live"
    assert MatchState.from_dict(data) == state


def test_from_dict_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="invalid match state"):
        MatchState.from_dict({"match_id": "x"})

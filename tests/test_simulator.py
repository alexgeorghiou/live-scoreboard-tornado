import pytest

from scoreboard.domain.events import parse_event
from scoreboard.domain.state import MatchState, apply_event
from scoreboard.simulator import MatchScript


@pytest.mark.parametrize("seed", range(25))
def test_generated_matches_are_legal(seed: int) -> None:
    script = MatchScript.create(seed)
    state = MatchState(match_id="sim", home_team="H", away_team="A")
    for seq, (_minute, payload) in enumerate(script.events(), start=1):
        state = apply_event(state, parse_event(payload).with_seq(seq))
    assert state.status == "finished"
    assert {"home": state.score.home, "away": state.score.away} == script.score


def test_scripts_are_deterministic_per_seed() -> None:
    a = list(MatchScript.create(7).events())
    b = list(MatchScript.create(7).events())
    assert a == b

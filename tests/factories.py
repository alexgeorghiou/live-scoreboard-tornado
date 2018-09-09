"""Small builders that keep test bodies focused on behaviour."""

from __future__ import annotations

from typing import Any

from scoreboard.domain.events import MatchEvent, parse_event
from scoreboard.domain.state import MatchState, apply_event


def new_state(match_id: str = "m1") -> MatchState:
    return MatchState(match_id=match_id, home_team="Rovers", away_team="United")


def ev(seq: int = 1, **payload: Any) -> MatchEvent:
    return parse_event(payload).with_seq(seq)


def kickoff(state: MatchState | None = None) -> MatchState:
    state = state or new_state()
    return apply_event(state, ev(state.version + 1, type="period_change", period="first_half"))


def play(state: MatchState, *payloads: dict[str, Any]) -> MatchState:
    for payload in payloads:
        state = apply_event(state, ev(state.version + 1, **payload))
    return state


def period(name: str, minute: int = 0) -> dict[str, Any]:
    return {"type": "period_change", "period": name, "minute": minute}


def goal(team: str, player: str, minute: int = 10, **extra: Any) -> dict[str, Any]:
    return {"type": "goal", "team": team, "player": player, "minute": minute, **extra}

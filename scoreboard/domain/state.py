"""Derived match state.

:class:`MatchState` is never edited directly. It is the left fold of a match's
event log through :func:`apply_event`, which keeps the rules of the game in a
single, pure, easily tested place. The same reducer runs on every instance, so
any node that replays the log reaches the same score.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from scoreboard.domain.events import (
    CardColour,
    EventType,
    MatchEvent,
    Period,
    Team,
)

MAX_SUBSTITUTIONS = 5
EXTRA_TIME_SUBSTITUTIONS = 1


class IllegalEventError(Exception):
    """The event is well formed but not legal for the current match state."""


_TRANSITIONS: dict[Period, frozenset[Period]] = {
    Period.PRE_MATCH: frozenset({Period.FIRST_HALF}),
    Period.FIRST_HALF: frozenset({Period.HALF_TIME}),
    Period.HALF_TIME: frozenset({Period.SECOND_HALF}),
    Period.SECOND_HALF: frozenset({Period.FULL_TIME, Period.EXTRA_TIME_BREAK}),
    Period.EXTRA_TIME_BREAK: frozenset({Period.EXTRA_TIME_FIRST}),
    Period.EXTRA_TIME_FIRST: frozenset({Period.EXTRA_TIME_HALF_TIME}),
    Period.EXTRA_TIME_HALF_TIME: frozenset({Period.EXTRA_TIME_SECOND}),
    Period.EXTRA_TIME_SECOND: frozenset({Period.FULL_TIME, Period.PENALTIES}),
    Period.PENALTIES: frozenset({Period.FULL_TIME}),
    Period.FULL_TIME: frozenset(),
}

_SCORING_PERIODS = frozenset(
    {Period.FIRST_HALF, Period.SECOND_HALF, Period.EXTRA_TIME_FIRST, Period.EXTRA_TIME_SECOND}
)
_EXTRA_TIME_PERIODS = frozenset(
    {
        Period.EXTRA_TIME_BREAK,
        Period.EXTRA_TIME_FIRST,
        Period.EXTRA_TIME_HALF_TIME,
        Period.EXTRA_TIME_SECOND,
        Period.PENALTIES,
    }
)


@dataclass(frozen=True, slots=True)
class Score:
    home: int = 0
    away: int = 0

    def get(self, team: Team) -> int:
        return self.home if team is Team.HOME else self.away

    def add(self, team: Team, delta: int) -> Score:
        if team is Team.HOME:
            return Score(self.home + delta, self.away)
        return Score(self.home, self.away + delta)

    @property
    def level(self) -> bool:
        return self.home == self.away


@dataclass(frozen=True, slots=True)
class GoalRecord:
    event_id: str
    team: Team
    player: str
    minute: int
    own_goal: bool = False
    penalty: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class CardRecord:
    event_id: str
    team: Team
    player: str
    colour: CardColour
    minute: int
    second_yellow: bool = False


@dataclass(frozen=True, slots=True)
class SubstitutionRecord:
    event_id: str
    team: Team
    player_off: str
    player_on: str
    minute: int


@dataclass(frozen=True, slots=True)
class MatchState:
    match_id: str
    home_team: str
    away_team: str
    competition: str = ""
    period: Period = Period.PRE_MATCH
    score: Score = field(default_factory=Score)
    goals: tuple[GoalRecord, ...] = ()
    cards: tuple[CardRecord, ...] = ()
    substitutions: tuple[SubstitutionRecord, ...] = ()
    minute: int = 0
    version: int = 0

    # ------------------------------------------------------------------ queries
    @property
    def status(self) -> str:
        if self.period is Period.PRE_MATCH:
            return "scheduled"
        if self.period is Period.FULL_TIME:
            return "finished"
        return "live" if self.period.in_play else "break"

    def booked(self, team: Team) -> set[str]:
        """Players currently on one yellow card."""
        yellows: dict[str, int] = {}
        for card in self.cards:
            if card.team is team and card.colour is CardColour.YELLOW:
                yellows[card.player] = yellows.get(card.player, 0) + 1
        return {p for p, n in yellows.items() if n == 1} - self.sent_off(team)

    def sent_off(self, team: Team) -> set[str]:
        return {c.player for c in self.cards if c.team is team and c.colour is CardColour.RED}

    def subbed_off(self, team: Team) -> set[str]:
        return {s.player_off for s in self.substitutions if s.team is team}

    def subbed_on(self, team: Team) -> set[str]:
        return {s.player_on for s in self.substitutions if s.team is team}

    def substitutions_used(self, team: Team) -> int:
        return sum(1 for s in self.substitutions if s.team is team)

    def substitution_limit(self) -> int:
        extra = EXTRA_TIME_SUBSTITUTIONS if self.period in _EXTRA_TIME_PERIODS else 0
        return MAX_SUBSTITUTIONS + extra

    # ------------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "competition": self.competition,
            "period": self.period.value,
            "status": self.status,
            "score": {"home": self.score.home, "away": self.score.away},
            "minute": self.minute,
            "version": self.version,
            "goals": [
                {
                    "event_id": g.event_id,
                    "team": g.team.value,
                    "player": g.player,
                    "minute": g.minute,
                    "own_goal": g.own_goal,
                    "penalty": g.penalty,
                    "cancelled": g.cancelled,
                }
                for g in self.goals
            ],
            "cards": [
                {
                    "event_id": c.event_id,
                    "team": c.team.value,
                    "player": c.player,
                    "colour": c.colour.value,
                    "minute": c.minute,
                    "second_yellow": c.second_yellow,
                }
                for c in self.cards
            ],
            "substitutions": [
                {
                    "event_id": s.event_id,
                    "team": s.team.value,
                    "player_off": s.player_off,
                    "player_on": s.player_on,
                    "minute": s.minute,
                }
                for s in self.substitutions
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MatchState:
        try:
            score = data.get("score") or {}
            return cls(
                match_id=str(data["match_id"]),
                home_team=str(data["home_team"]),
                away_team=str(data["away_team"]),
                competition=str(data.get("competition", "")),
                period=Period(data.get("period", Period.PRE_MATCH)),
                score=Score(int(score.get("home", 0)), int(score.get("away", 0))),
                minute=int(data.get("minute", 0)),
                version=int(data.get("version", 0)),
                goals=tuple(
                    GoalRecord(
                        event_id=str(g["event_id"]),
                        team=Team(g["team"]),
                        player=str(g["player"]),
                        minute=int(g["minute"]),
                        own_goal=bool(g.get("own_goal", False)),
                        penalty=bool(g.get("penalty", False)),
                        cancelled=bool(g.get("cancelled", False)),
                    )
                    for g in data.get("goals", [])
                ),
                cards=tuple(
                    CardRecord(
                        event_id=str(c["event_id"]),
                        team=Team(c["team"]),
                        player=str(c["player"]),
                        colour=CardColour(c["colour"]),
                        minute=int(c["minute"]),
                        second_yellow=bool(c.get("second_yellow", False)),
                    )
                    for c in data.get("cards", [])
                ),
                substitutions=tuple(
                    SubstitutionRecord(
                        event_id=str(s["event_id"]),
                        team=Team(s["team"]),
                        player_off=str(s["player_off"]),
                        player_on=str(s["player_on"]),
                        minute=int(s["minute"]),
                    )
                    for s in data.get("substitutions", [])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid match state: {exc}") from exc


# ---------------------------------------------------------------------- reducer
def apply_event(state: MatchState, event: MatchEvent) -> MatchState:
    """Return the state that results from applying ``event`` to ``state``."""
    if state.period is Period.FULL_TIME:
        raise IllegalEventError("match has finished")

    handler = _HANDLERS[event.type]
    new_state = handler(state, event)
    return replace(new_state, minute=max(state.minute, event.minute), version=event.seq)


def derive_state(initial: MatchState, events: Iterable[MatchEvent]) -> MatchState:
    """Fold an event log into a state, starting from ``initial``."""
    state = initial
    for event in events:
        state = apply_event(state, event)
    return state


def _team(event: MatchEvent) -> Team:
    assert event.team is not None  # guaranteed by parse_event
    return event.team


def _apply_goal(state: MatchState, event: MatchEvent) -> MatchState:
    if state.period not in _SCORING_PERIODS:
        raise IllegalEventError(f"goals cannot be scored during {state.period.value}")
    team = _team(event)
    player = event.player or ""
    # For an own goal the scorer plays for the opposing side.
    scorer_side = team.opponent if event.own_goal else team
    if player in state.sent_off(scorer_side):
        raise IllegalEventError(f"{player} has been sent off")
    if player in state.subbed_off(scorer_side):
        raise IllegalEventError(f"{player} has been substituted off")
    goal = GoalRecord(
        event_id=event.id,
        team=team,
        player=player,
        minute=event.minute,
        own_goal=event.own_goal,
        penalty=event.penalty,
    )
    return replace(state, score=state.score.add(team, 1), goals=(*state.goals, goal))


def _apply_goal_cancelled(state: MatchState, event: MatchEvent) -> MatchState:
    for index, goal in enumerate(state.goals):
        if goal.event_id != event.ref_event_id:
            continue
        if goal.cancelled:
            raise IllegalEventError("goal has already been cancelled")
        goals = list(state.goals)
        goals[index] = replace(goal, cancelled=True)
        return replace(state, score=state.score.add(goal.team, -1), goals=tuple(goals))
    raise IllegalEventError(f"no goal with id {event.ref_event_id}")


def _apply_card(state: MatchState, event: MatchEvent) -> MatchState:
    if state.period in (Period.PRE_MATCH,):
        raise IllegalEventError("cards cannot be shown before kick-off")
    team = _team(event)
    player = event.player or ""
    if player in state.sent_off(team):
        raise IllegalEventError(f"{player} has already been sent off")

    records = [
        CardRecord(
            event_id=event.id,
            team=team,
            player=player,
            colour=event.card or CardColour.YELLOW,
            minute=event.minute,
        )
    ]
    if event.card is CardColour.YELLOW and player in state.booked(team):
        # Second booking: the yellow stands and an automatic red follows.
        records.append(
            CardRecord(
                event_id=f"{event.id}:red",
                team=team,
                player=player,
                colour=CardColour.RED,
                minute=event.minute,
                second_yellow=True,
            )
        )
    return replace(state, cards=(*state.cards, *records))


def _apply_substitution(state: MatchState, event: MatchEvent) -> MatchState:
    if state.period is Period.PRE_MATCH:
        raise IllegalEventError("substitutions cannot be made before kick-off")
    team = _team(event)
    off, on = event.player_off or "", event.player_on or ""
    if state.substitutions_used(team) >= state.substitution_limit():
        raise IllegalEventError(f"{team.value} has no substitutions left")
    if off in state.sent_off(team):
        raise IllegalEventError(f"{off} has been sent off and cannot be substituted")
    if off in state.subbed_off(team):
        raise IllegalEventError(f"{off} has already been substituted off")
    if on in state.subbed_off(team):
        raise IllegalEventError(f"{on} was substituted off and cannot return")
    if on in state.subbed_on(team):
        raise IllegalEventError(f"{on} is already on the pitch")
    record = SubstitutionRecord(
        event_id=event.id, team=team, player_off=off, player_on=on, minute=event.minute
    )
    return replace(state, substitutions=(*state.substitutions, record))


def _apply_period_change(state: MatchState, event: MatchEvent) -> MatchState:
    target = event.period
    assert target is not None
    if target not in _TRANSITIONS[state.period]:
        raise IllegalEventError(f"cannot move from {state.period.value} to {target.value}")
    if target in (Period.EXTRA_TIME_BREAK, Period.PENALTIES) and not state.score.level:
        raise IllegalEventError(f"{target.value} requires the scores to be level")
    if (
        target is Period.FULL_TIME
        and state.period is Period.EXTRA_TIME_SECOND
        and state.score.level
    ):
        raise IllegalEventError("a level match after extra time goes to penalties")
    return replace(state, period=target)


_HANDLERS = {
    EventType.GOAL: _apply_goal,
    EventType.GOAL_CANCELLED: _apply_goal_cancelled,
    EventType.CARD: _apply_card,
    EventType.SUBSTITUTION: _apply_substitution,
    EventType.PERIOD_CHANGE: _apply_period_change,
}

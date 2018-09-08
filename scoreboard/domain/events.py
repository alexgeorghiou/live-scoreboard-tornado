"""Match event model and payload validation.

Events arrive as JSON from the REST API (or from an upstream feed) and are
validated into immutable :class:`MatchEvent` instances before they ever touch
match state. Validation here is purely structural; whether an event is legal
*for the current state of the match* is decided by the reducer.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ValidationError(ValueError):
    """Raised when an event payload is structurally invalid."""


class EventType(StrEnum):
    GOAL = "goal"
    GOAL_CANCELLED = "goal_cancelled"
    CARD = "card"
    SUBSTITUTION = "substitution"
    PERIOD_CHANGE = "period_change"


class Team(StrEnum):
    HOME = "home"
    AWAY = "away"

    @property
    def opponent(self) -> Team:
        return Team.AWAY if self is Team.HOME else Team.HOME


class CardColour(StrEnum):
    YELLOW = "yellow"
    RED = "red"


class Period(StrEnum):
    PRE_MATCH = "pre_match"
    FIRST_HALF = "first_half"
    HALF_TIME = "half_time"
    SECOND_HALF = "second_half"
    EXTRA_TIME_BREAK = "extra_time_break"
    EXTRA_TIME_FIRST = "extra_time_first"
    EXTRA_TIME_HALF_TIME = "extra_time_half_time"
    EXTRA_TIME_SECOND = "extra_time_second"
    PENALTIES = "penalties"
    FULL_TIME = "full_time"

    @property
    def in_play(self) -> bool:
        return self in _IN_PLAY


_IN_PLAY = frozenset(
    {
        Period.FIRST_HALF,
        Period.SECOND_HALF,
        Period.EXTRA_TIME_FIRST,
        Period.EXTRA_TIME_SECOND,
        Period.PENALTIES,
    }
)

MAX_MINUTE = 150


@dataclass(frozen=True, slots=True)
class MatchEvent:
    """A single fact about a match, as reported by the feed."""

    type: EventType
    minute: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    team: Team | None = None
    player: str | None = None
    # goal
    own_goal: bool = False
    penalty: bool = False
    # goal_cancelled
    ref_event_id: str | None = None
    # card
    card: CardColour | None = None
    # substitution
    player_off: str | None = None
    player_on: str | None = None
    # period_change
    period: Period | None = None
    # assigned by the repository when the event is accepted
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        return {k: (str(v) if isinstance(v, StrEnum) else v) for k, v in raw.items() if v is not None}

    def with_seq(self, seq: int) -> MatchEvent:
        from dataclasses import replace

        return replace(self, seq=seq)


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"'{key}' must be a non-empty string")
    return value.strip()


def _enum(enum_cls: type[StrEnum], data: dict[str, Any], key: str) -> Any:
    value = data.get(key)
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)  # type: ignore[attr-defined]
        raise ValidationError(f"'{key}' must be one of: {allowed}") from None


def parse_event(data: Any) -> MatchEvent:
    """Validate a raw JSON payload and build a :class:`MatchEvent`."""
    if not isinstance(data, dict):
        raise ValidationError("event payload must be a JSON object")

    etype: EventType = _enum(EventType, data, "type")

    minute = data.get("minute", 0)
    if isinstance(minute, bool) or not isinstance(minute, int):
        raise ValidationError("'minute' must be an integer")
    if not 0 <= minute <= MAX_MINUTE:
        raise ValidationError(f"'minute' must be between 0 and {MAX_MINUTE}")

    kwargs: dict[str, Any] = {"type": etype, "minute": minute}
    if "id" in data:
        kwargs["id"] = _require_str(data, "id")

    if etype is EventType.GOAL:
        kwargs["team"] = _enum(Team, data, "team")
        kwargs["player"] = _require_str(data, "player")
        kwargs["own_goal"] = bool(data.get("own_goal", False))
        kwargs["penalty"] = bool(data.get("penalty", False))
        if kwargs["own_goal"] and kwargs["penalty"]:
            raise ValidationError("a goal cannot be both an own goal and a penalty")
    elif etype is EventType.GOAL_CANCELLED:
        kwargs["ref_event_id"] = _require_str(data, "ref_event_id")
    elif etype is EventType.CARD:
        kwargs["team"] = _enum(Team, data, "team")
        kwargs["player"] = _require_str(data, "player")
        kwargs["card"] = _enum(CardColour, data, "card")
    elif etype is EventType.SUBSTITUTION:
        kwargs["team"] = _enum(Team, data, "team")
        kwargs["player_off"] = _require_str(data, "player_off")
        kwargs["player_on"] = _require_str(data, "player_on")
        if kwargs["player_off"] == kwargs["player_on"]:
            raise ValidationError("'player_off' and 'player_on' must differ")
    elif etype is EventType.PERIOD_CHANGE:
        kwargs["period"] = _enum(Period, data, "period")

    return MatchEvent(**kwargs)


def event_from_dict(data: dict[str, Any]) -> MatchEvent:
    """Rehydrate a previously accepted event (keeps its ``seq``)."""
    event = parse_event(data)
    return event.with_seq(int(data.get("seq", 0)))

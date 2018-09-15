"""Dictionary-backed repository for tests and single-node development."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from scoreboard.domain.events import MatchEvent
from scoreboard.domain.state import MatchState, apply_event
from scoreboard.repository.base import (
    AppendResult,
    MatchExistsError,
    MatchNotFoundError,
    MatchRepository,
    VersionConflictError,
)


@dataclass
class _Record:
    base: MatchState
    state: MatchState
    events: list[MatchEvent] = field(default_factory=list)
    seen_ids: dict[str, MatchEvent] = field(default_factory=dict)


class InMemoryMatchRepository(MatchRepository):
    """Each method runs without awaiting, so it is atomic on the event loop."""

    def __init__(self) -> None:
        self._records: dict[str, _Record] = {}

    def _record(self, match_id: str) -> _Record:
        try:
            return self._records[match_id]
        except KeyError:
            raise MatchNotFoundError(match_id) from None

    async def create(self, state: MatchState) -> MatchState:
        if state.match_id in self._records:
            raise MatchExistsError(state.match_id)
        self._records[state.match_id] = _Record(base=state, state=state)
        return state

    async def get(self, match_id: str) -> MatchState:
        return self._record(match_id).state

    async def list(self) -> list[MatchState]:
        return [r.state for r in self._records.values()]

    async def append(
        self, match_id: str, event: MatchEvent, expected_version: int | None = None
    ) -> AppendResult:
        record = self._record(match_id)
        if event.id in record.seen_ids:
            return AppendResult(record.state, record.seen_ids[event.id], duplicate=True)
        current = record.state
        if expected_version is not None and expected_version != current.version:
            raise VersionConflictError(expected_version, current.version)
        accepted = event.with_seq(current.version + 1)
        new_state = apply_event(current, accepted)  # raises before anything is mutated
        record.state = new_state
        record.events.append(accepted)
        record.seen_ids[accepted.id] = accepted
        return AppendResult(new_state, accepted)

    async def events(self, match_id: str, since: int = 0) -> list[MatchEvent]:
        return [e for e in self._record(match_id).events if e.seq > since]

    async def replace(self, state: MatchState) -> MatchState:
        record = self._record(state.match_id)
        installed = replace(state, version=record.state.version + 1)
        record.base = installed
        record.state = installed
        record.events.clear()
        return installed

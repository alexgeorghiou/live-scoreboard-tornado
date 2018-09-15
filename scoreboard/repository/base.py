"""Match persistence: an event log plus the state derived from it.

Each match is stored as a *base* state, the events accepted since that base and
the current state (the cached fold of the two). Creating a match sets the base
to the pre-match state; an authoritative snapshot from the feed replaces the
base and clears the tail, so ``derive_state(base, events) == state`` always
holds.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from scoreboard.domain.events import MatchEvent
from scoreboard.domain.state import MatchState


class MatchNotFoundError(LookupError):
    def __init__(self, match_id: str) -> None:
        super().__init__(f"match {match_id!r} not found")
        self.match_id = match_id


class MatchExistsError(Exception):
    def __init__(self, match_id: str) -> None:
        super().__init__(f"match {match_id!r} already exists")
        self.match_id = match_id


class VersionConflictError(Exception):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected version {expected}, match is at {actual}")
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class AppendResult:
    state: MatchState
    event: MatchEvent
    duplicate: bool = False


class MatchRepository(ABC):
    @abstractmethod
    async def create(self, state: MatchState) -> MatchState: ...

    @abstractmethod
    async def get(self, match_id: str) -> MatchState: ...

    @abstractmethod
    async def list(self) -> list[MatchState]: ...

    @abstractmethod
    async def append(
        self, match_id: str, event: MatchEvent, expected_version: int | None = None
    ) -> AppendResult:
        """Apply ``event`` and persist it.

        Re-submitting an event id that was already accepted is a no-op that
        returns the current state with ``duplicate=True``, which makes feed
        retries safe.
        """

    @abstractmethod
    async def events(self, match_id: str, since: int = 0) -> list[MatchEvent]:
        """Events after the current base with ``seq > since``."""

    @abstractmethod
    async def replace(self, state: MatchState) -> MatchState:
        """Install ``state`` as the new base at the next version number."""

    async def close(self) -> None:  # noqa: B027
        """Release any resources held by the repository."""

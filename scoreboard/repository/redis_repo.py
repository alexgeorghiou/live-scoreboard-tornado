"""Redis-backed repository so several scoreboard instances share match state.

Layout (``{p}`` is the key prefix)::

    {p}:matches              SET   of match ids
    {p}:match:{id}:state     STRING JSON of the current MatchState
    {p}:match:{id}:base      STRING JSON of the base MatchState
    {p}:match:{id}:events    LIST  of JSON events since the base
    {p}:match:{id}:ids       HASH  event id -> JSON event (idempotency)

Appends use optimistic locking (``WATCH`` the state key, apply the reducer in
Python, ``MULTI``/``EXEC`` the writes) and retry on contention. The reducer has
to run in Python, so a Lua script is not an option without duplicating rules.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import WatchError

from scoreboard.domain.events import MatchEvent, event_from_dict
from scoreboard.domain.state import MatchState, apply_event
from scoreboard.repository.base import (
    AppendResult,
    MatchExistsError,
    MatchNotFoundError,
    MatchRepository,
    VersionConflictError,
)

MAX_RETRIES = 20


class RetryExhaustedError(RuntimeError):
    pass


class RedisMatchRepository(MatchRepository):
    def __init__(self, client: Redis, prefix: str = "scoreboard") -> None:
        self._r = client
        self._p = prefix

    @classmethod
    def from_url(cls, url: str, prefix: str = "scoreboard") -> RedisMatchRepository:
        return cls(Redis.from_url(url, decode_responses=True), prefix)

    # ----------------------------------------------------------------- keys
    def _k(self, match_id: str, suffix: str) -> str:
        return f"{self._p}:match:{match_id}:{suffix}"

    @property
    def _index(self) -> str:
        return f"{self._p}:matches"

    # ------------------------------------------------------------- commands
    async def create(self, state: MatchState) -> MatchState:
        blob = _dump(state.to_dict())
        created = await self._r.set(self._k(state.match_id, "state"), blob, nx=True)
        if not created:
            raise MatchExistsError(state.match_id)
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.set(self._k(state.match_id, "base"), blob)
            pipe.sadd(self._index, state.match_id)
            await pipe.execute()
        return state

    async def get(self, match_id: str) -> MatchState:
        raw = await self._r.get(self._k(match_id, "state"))
        if raw is None:
            raise MatchNotFoundError(match_id)
        return MatchState.from_dict(json.loads(raw))

    async def list(self) -> list[MatchState]:
        ids = sorted(await self._r.smembers(self._index))
        if not ids:
            return []
        blobs = await self._r.mget([self._k(i, "state") for i in ids])
        return [MatchState.from_dict(json.loads(b)) for b in blobs if b is not None]

    async def append(
        self, match_id: str, event: MatchEvent, expected_version: int | None = None
    ) -> AppendResult:
        state_key = self._k(match_id, "state")
        ids_key = self._k(match_id, "ids")
        for _ in range(MAX_RETRIES):
            async with self._r.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(state_key, ids_key)
                    raw = await pipe.get(state_key)
                    if raw is None:
                        raise MatchNotFoundError(match_id)
                    current = MatchState.from_dict(json.loads(raw))
                    seen = await pipe.hget(ids_key, event.id)
                    if seen is not None:
                        return AppendResult(
                            current, event_from_dict(json.loads(seen)), duplicate=True
                        )
                    if expected_version is not None and expected_version != current.version:
                        raise VersionConflictError(expected_version, current.version)

                    accepted = event.with_seq(current.version + 1)
                    new_state = apply_event(current, accepted)
                    event_blob = _dump(accepted.to_dict())

                    pipe.multi()
                    pipe.set(state_key, _dump(new_state.to_dict()))
                    pipe.rpush(self._k(match_id, "events"), event_blob)
                    pipe.hset(ids_key, accepted.id, event_blob)
                    await pipe.execute()
                    return AppendResult(new_state, accepted)
                except WatchError:
                    continue
        raise RetryExhaustedError(f"could not append to {match_id} after {MAX_RETRIES} tries")

    async def events(self, match_id: str, since: int = 0) -> list[MatchEvent]:
        if not await self._r.exists(self._k(match_id, "state")):
            raise MatchNotFoundError(match_id)
        raw = await self._r.lrange(self._k(match_id, "events"), 0, -1)
        events = [event_from_dict(json.loads(item)) for item in raw]
        return [e for e in events if e.seq > since]

    async def replace(self, state: MatchState) -> MatchState:
        state_key = self._k(state.match_id, "state")
        for _ in range(MAX_RETRIES):
            async with self._r.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(state_key)
                    raw = await pipe.get(state_key)
                    if raw is None:
                        raise MatchNotFoundError(state.match_id)
                    current_version = int(json.loads(raw)["version"])
                    installed = replace(state, version=current_version + 1)
                    blob = _dump(installed.to_dict())
                    pipe.multi()
                    pipe.set(state_key, blob)
                    pipe.set(self._k(state.match_id, "base"), blob)
                    pipe.delete(self._k(state.match_id, "events"))
                    await pipe.execute()
                    return installed
                except WatchError:
                    continue
        raise RetryExhaustedError(f"could not replace {state.match_id}")

    async def close(self) -> None:
        await self._r.aclose()


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)

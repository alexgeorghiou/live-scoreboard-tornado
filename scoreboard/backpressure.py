"""Per-client outbound buffering with a hard bound.

A broadcast server is only as fast as its slowest consumer unless every client
gets its own bounded buffer. :class:`Outbox` sits between the fan-out (which
must never block) and the socket writer (which may block on a slow peer).

When the buffer is full the configured :class:`OverflowPolicy` decides:

``disconnect``
    Close the client straight away. Simple and predictable; the client
    reconnects and receives a fresh snapshot.

``drop_oldest``
    Make room by discarding a queued message. Because every update carries
    the full match state, a message that is followed by a newer one for the
    same match is pure redundancy and is discarded first (conflation). Only if
    no such message exists is the oldest message dropped, and its match is
    marked *stale* so the writer can send a fresh snapshot once the client
    catches up. A client that keeps overflowing (``max_drops`` drops without
    a single successful write in between) is disconnected anyway.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import StrEnum


class OverflowPolicy(StrEnum):
    DROP_OLDEST = "drop_oldest"
    DISCONNECT = "disconnect"


class OutboxClosedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Envelope:
    key: str
    version: int
    payload: str


@dataclass(slots=True)
class OutboxStats:
    enqueued: int = 0
    delivered: int = 0
    dropped: int = 0
    conflated: int = 0
    skipped_stale_versions: int = 0


class Outbox:
    def __init__(
        self,
        maxsize: int = 64,
        policy: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
        max_drops: int = 256,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self.policy = policy
        self.max_drops = max_drops
        self.stats = OutboxStats()
        self._items: deque[Envelope] = deque()
        self._versions: dict[str, int] = {}
        self._stale: set[str] = set()
        self._ready = asyncio.Event()
        self._closed = False
        self._drops_since_delivery = 0
        self.overflowed = False

    def __len__(self) -> int:
        return len(self._items)

    @property
    def closed(self) -> bool:
        return self._closed

    def offer(self, key: str, version: int, payload: str) -> bool:
        """Queue a message without ever blocking.

        Returns ``False`` when the client should be disconnected.
        """
        if self._closed:
            return False
        if version and version <= self._versions.get(key, 0):
            # Already queued or sent something at least this new for the match.
            self.stats.skipped_stale_versions += 1
            return True
        if len(self._items) >= self.maxsize and not self._make_room(key):
            self.overflowed = True
            self.close()
            return False
        self._items.append(Envelope(key, version, payload))
        if version:
            self._versions[key] = version
        self.stats.enqueued += 1
        self._ready.set()
        return True

    def _make_room(self, incoming_key: str) -> bool:
        if self.policy is OverflowPolicy.DISCONNECT:
            return False
        self._drops_since_delivery += 1
        if self.max_drops and self._drops_since_delivery > self.max_drops:
            return False
        # 1) Conflate: the incoming message supersedes the oldest one for its key.
        newer_keys = {incoming_key}
        for idx in range(len(self._items) - 1, -1, -1):
            item = self._items[idx]
            if item.key in newer_keys and self._has_later(idx, item.key, incoming_key):
                del self._items[idx]
                self.stats.conflated += 1
                return True
            newer_keys.add(item.key)
        # 2) Otherwise shed the oldest message and remember the match is stale.
        victim = self._items.popleft()
        self._stale.add(victim.key)
        self.stats.dropped += 1
        return True

    def _has_later(self, idx: int, key: str, incoming_key: str) -> bool:
        if key == incoming_key:
            return True
        return any(self._items[j].key == key for j in range(idx + 1, len(self._items)))

    async def get(self) -> Envelope:
        while not self._items:
            if self._closed:
                raise OutboxClosedError
            self._ready.clear()
            await self._ready.wait()
        return self._items.popleft()

    def mark_delivered(self) -> None:
        self.stats.delivered += 1
        self._drops_since_delivery = 0

    def take_stale(self) -> set[str]:
        """Matches whose updates were shed; the caller should resend snapshots."""
        stale, self._stale = self._stale, set()
        return stale

    def forget(self, key: str) -> None:
        """Drop version tracking for a match (e.g. after unsubscribe)."""
        self._versions.pop(key, None)
        self._stale.discard(key)
        self._items = deque(i for i in self._items if i.key != key)

    def close(self) -> None:
        self._closed = True
        self._ready.set()

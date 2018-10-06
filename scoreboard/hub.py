"""Local fan-out of broker messages to the WebSocket clients of one process.

The hub keeps exactly one broker subscription per match that has at least one
local client, however many clients are watching it. A message arriving from the
broker is parsed once, then offered to each client's bounded outbox. Offering
never blocks; a client whose outbox refuses the message is evicted, so one slow
reader can never delay the others.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Protocol

from scoreboard.broker import Broker, Subscription, match_channel
from scoreboard.domain.state import MatchState
from scoreboard.repository import MatchRepository

log = logging.getLogger(__name__)


class Subscriber(Protocol):
    client_id: str

    def deliver(self, match_id: str, version: int, payload: str) -> bool:
        """Queue a message; return ``False`` if the client must be dropped."""
        ...

    def evict(self, reason: str) -> None: ...


@dataclass(slots=True)
class HubStats:
    messages_in: int = 0
    deliveries: int = 0
    evictions: int = 0
    malformed: int = 0


class Hub:
    def __init__(self, broker: Broker, repository: MatchRepository) -> None:
        self._broker = broker
        self._repository = repository
        self._subscribers: dict[str, set[Subscriber]] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._lock = asyncio.Lock()
        self.stats = HubStats()

    # ---------------------------------------------------------------- queries
    def watchers(self, match_id: str) -> int:
        return len(self._subscribers.get(match_id, ()))

    @property
    def matches(self) -> list[str]:
        return sorted(self._subscribers)

    # ------------------------------------------------------------- lifecycle
    async def subscribe(self, subscriber: Subscriber, match_id: str) -> MatchState:
        """Register interest and return the current state for the initial snapshot.

        The broker subscription is established *before* the state is read, so
        an event published in between is either reflected in the returned
        state or delivered afterwards; clients discard anything older than
        the version they hold.
        """
        await self._repository.get(match_id)  # fail fast on unknown matches
        async with self._lock:
            if match_id not in self._subscriptions:
                self._subscriptions[match_id] = await self._broker.subscribe(
                    match_channel(match_id), self._on_message
                )
            self._subscribers.setdefault(match_id, set()).add(subscriber)
        return await self._repository.get(match_id)

    async def unsubscribe(self, subscriber: Subscriber, match_id: str) -> None:
        async with self._lock:
            subs = self._subscribers.get(match_id)
            if subs is None:
                return
            subs.discard(subscriber)
            if subs:
                return
            del self._subscribers[match_id]
            subscription = self._subscriptions.pop(match_id, None)
        if subscription is not None:
            await subscription.cancel()

    async def remove(self, subscriber: Subscriber) -> None:
        for match_id in [m for m, subs in self._subscribers.items() if subscriber in subs]:
            await self.unsubscribe(subscriber, match_id)

    async def close(self) -> None:
        async with self._lock:
            subscriptions = list(self._subscriptions.values())
            self._subscriptions.clear()
            self._subscribers.clear()
        for subscription in subscriptions:
            await subscription.cancel()

    # --------------------------------------------------------------- fan-out
    def _on_message(self, channel: str, raw: str) -> None:
        self.stats.messages_in += 1
        try:
            message = json.loads(raw)
            match_id = str(message["match_id"])
            version = int(message.get("version", 0))
        except (ValueError, KeyError, TypeError):
            self.stats.malformed += 1
            log.warning("dropping malformed broker message on %s", channel)
            return
        for subscriber in list(self._subscribers.get(match_id, ())):
            if subscriber.deliver(match_id, version, raw):
                self.stats.deliveries += 1
            else:
                self.stats.evictions += 1
                self._subscribers[match_id].discard(subscriber)
                subscriber.evict("slow consumer")

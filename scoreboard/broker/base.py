"""Broker abstraction used to fan match updates out across server instances.

Every scoreboard instance publishes accepted events to a per-match channel and
subscribes to the channels its connected clients care about. The interface is
deliberately tiny so it can be backed by Redis in production and by a plain
in-process dictionary in tests and local development.
"""

from __future__ import annotations

import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

MessageHandler = Callable[[str, str], Awaitable[None] | None]
"""Called with ``(channel, message)`` for every message on a subscribed channel."""


def match_channel(match_id: str) -> str:
    return f"scoreboard:match:{match_id}"


class Subscription:
    """Handle returned by :meth:`Broker.subscribe`; call :meth:`cancel` to stop."""

    def __init__(self, broker: Broker, channel: str, handler: MessageHandler) -> None:
        self.broker = broker
        self.channel = channel
        self.handler = handler
        self.active = True

    async def cancel(self) -> None:
        if self.active:
            self.active = False
            await self.broker._unsubscribe(self)


class Broker(ABC):
    """Publish/subscribe transport for opaque string messages."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Subscription]] = {}

    @abstractmethod
    async def publish(self, channel: str, message: str) -> None: ...

    async def subscribe(self, channel: str, handler: MessageHandler) -> Subscription:
        sub = Subscription(self, channel, handler)
        subs = self._handlers.setdefault(channel, [])
        subs.append(sub)
        if len(subs) == 1:
            await self._on_first_subscriber(channel)
        return sub

    async def _unsubscribe(self, sub: Subscription) -> None:
        subs = self._handlers.get(sub.channel)
        if not subs or sub not in subs:
            return
        subs.remove(sub)
        if not subs:
            del self._handlers[sub.channel]
            await self._on_last_unsubscribed(sub.channel)

    @property
    def channels(self) -> set[str]:
        return set(self._handlers)

    async def _on_first_subscriber(self, channel: str) -> None:  # noqa: B027
        """Hook for transports that need to register interest upstream."""

    async def _on_last_unsubscribed(self, channel: str) -> None:  # noqa: B027
        """Hook for transports that need to drop interest upstream."""

    async def _dispatch(self, channel: str, message: str) -> None:
        for sub in list(self._handlers.get(channel, ())):
            if not sub.active:
                continue
            try:
                result = sub.handler(channel, message)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # One misbehaving subscriber must not starve the others.
                log.exception("broker handler failed for channel %s", channel)

    async def close(self) -> None:
        self._handlers.clear()

"""Redis pub/sub broker.

One :class:`redis.asyncio.client.PubSub` connection per process carries every
channel this instance is interested in. Subscriptions are reference counted by
the base class: Redis only hears ``SUBSCRIBE`` for the first local listener of a
match and ``UNSUBSCRIBE`` when the last one leaves.

A single reader task pulls messages and dispatches them in order. If the
connection drops, the reader backs off, reconnects and re-subscribes to every
channel that still has local listeners.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from scoreboard.broker.base import Broker

log = logging.getLogger(__name__)


class RedisBroker(Broker):
    def __init__(
        self,
        client: Redis,
        *,
        poll_timeout: float = 1.0,
        max_backoff: float = 5.0,
    ) -> None:
        super().__init__()
        self._client = client
        self._pubsub: Any = client.pubsub(ignore_subscribe_messages=True)
        self._poll_timeout = poll_timeout
        self._max_backoff = max_backoff
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisBroker:
        return cls(Redis.from_url(url, decode_responses=True), **kwargs)

    async def publish(self, channel: str, message: str) -> None:
        await self._client.publish(channel, message)

    async def _on_first_subscriber(self, channel: str) -> None:
        async with self._lock:
            await self._pubsub.subscribe(channel)
        self._ensure_reader()

    async def _on_last_unsubscribed(self, channel: str) -> None:
        async with self._lock:
            await self._pubsub.unsubscribe(channel)

    def _ensure_reader(self) -> None:
        if self._reader is None or self._reader.done():
            self._reader = asyncio.create_task(self._read_loop(), name="redis-broker-reader")

    async def _read_loop(self) -> None:
        backoff = 0.1
        while not self._closed:
            try:
                if not self._pubsub.subscribed:
                    await asyncio.sleep(self._poll_timeout / 10)
                    continue
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=self._poll_timeout
                )
                backoff = 0.1
                if message is None or message.get("type") != "message":
                    continue
                await self._dispatch(_text(message["channel"]), _text(message["data"]))
            except asyncio.CancelledError:
                raise
            except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
                log.warning("redis pub/sub connection lost (%s); retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                await self._resubscribe()
            except Exception:
                log.exception("unexpected error in redis broker reader")
                await asyncio.sleep(backoff)

    async def _resubscribe(self) -> None:
        channels = sorted(self.channels)
        if not channels:
            return
        try:
            async with self._lock:
                await self._pubsub.subscribe(*channels)
        except (RedisConnectionError, RedisTimeoutError, OSError):
            log.debug("resubscribe failed; will retry")

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except (RedisConnectionError, RedisTimeoutError, OSError):
            return False

    async def close(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        with contextlib.suppress(Exception):
            await self._pubsub.aclose()
        await super().close()


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value

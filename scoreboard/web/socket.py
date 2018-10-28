"""WebSocket endpoint: per-match subscriptions, heartbeats and backpressure.

Client -> server (JSON text frames)::

    {"op": "subscribe",   "match_id": "derby"}
    {"op": "unsubscribe", "match_id": "derby"}
    {"op": "resync",      "match_id": "derby"}
    {"op": "ping", "ts": 1712345678901}

Server -> client::

    {"type": "hello", "client_id": ..., "ping_interval": ..., "idle_timeout": ...}
    {"type": "snapshot", "match_id": ..., "version": n, "reason": ..., "state": {...}}
    {"type": "event",    "match_id": ..., "version": n, "event": {...}, "state": {...}}
    {"type": "unsubscribed", "match_id": ...}
    {"type": "pong", "ts": ...}
    {"type": "error", "message": ...}

Each connection owns two tasks: a *writer* that drains the bounded outbox
into the socket one frame at a time (so a slow peer only ever stalls its own
writer) and a *heartbeat* that pings and enforces the idle timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any
from urllib.parse import urlparse

from tornado.websocket import WebSocketClosedError, WebSocketHandler

from scoreboard.backpressure import Outbox, OutboxClosedError
from scoreboard.context import AppContext
from scoreboard.heartbeat import Heartbeat
from scoreboard.messages import encode, error_message, snapshot_message
from scoreboard.repository import MatchNotFoundError

log = logging.getLogger(__name__)

CLOSE_IDLE_TIMEOUT = 4000
CLOSE_SLOW_CONSUMER = 4001
CLOSE_WRITE_TIMEOUT = 4002

CONTROL_KEY = "_control"


class ScoreboardSocket(WebSocketHandler):
    ctx: AppContext

    def initialize(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.client_id = uuid.uuid4().hex[:12]
        self.subscriptions: set[str] = set()
        self.outbox = Outbox(
            maxsize=ctx.settings.outbox_size,
            policy=ctx.settings.overflow_policy,
            max_drops=ctx.settings.max_drops,
        )
        self.heartbeat = Heartbeat(ctx.settings.ping_interval, ctx.settings.idle_timeout)
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False

    # --------------------------------------------------------------- handshake
    def check_origin(self, origin: str) -> bool:
        allowed = self.ctx.settings.allowed_origins
        if "*" in allowed:
            return True
        if allowed:
            return origin in allowed
        # Default: same host only (what Tornado does), plus localhost in debug.
        host = urlparse(origin).hostname or ""
        if self.ctx.settings.debug and host in {"localhost", "127.0.0.1"}:
            return True
        return super().check_origin(origin)

    async def open(self) -> None:  # type: ignore[override]
        self.ctx.connections.add(self)
        self.heartbeat.touch()
        self._tasks = [
            asyncio.create_task(self._write_loop(), name=f"ws-writer-{self.client_id}"),
            asyncio.create_task(self._heartbeat_loop(), name=f"ws-heartbeat-{self.client_id}"),
        ]
        self.send_control(
            {
                "type": "hello",
                "client_id": self.client_id,
                "ping_interval": self.ctx.settings.ping_interval,
                "idle_timeout": self.ctx.settings.idle_timeout,
                "max_subscriptions": self.ctx.settings.max_subscriptions,
            }
        )
        for match_id in filter(None, self.get_query_argument("match", "").split(",")):
            await self._subscribe(match_id.strip())

    # ----------------------------------------------------------------- inbound
    async def on_message(self, message: str | bytes) -> None:
        self.heartbeat.touch()
        try:
            data = json.loads(message)
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            self.send_control(error_message("messages must be JSON objects"))
            return

        op = data.get("op")
        match_id = data.get("match_id")
        if op == "ping":
            self.send_control({"type": "pong", "ts": data.get("ts")})
        elif op in {"subscribe", "unsubscribe", "resync"}:
            if not isinstance(match_id, str) or not match_id:
                self.send_control(error_message(f"'{op}' requires a match_id"))
            elif op == "subscribe":
                await self._subscribe(match_id)
            elif op == "unsubscribe":
                await self._unsubscribe(match_id)
            else:
                await self._resync(match_id)
        else:
            self.send_control(error_message(f"unknown op {op!r}"))

    def on_pong(self, data: bytes) -> None:
        self.heartbeat.record_pong()

    def on_ping(self, data: bytes) -> None:
        self.heartbeat.touch()

    async def _subscribe(self, match_id: str) -> None:
        if match_id in self.subscriptions:
            await self._resync(match_id, reason="subscribe")
            return
        if len(self.subscriptions) >= self.ctx.settings.max_subscriptions:
            self.send_control(error_message("subscription limit reached", match_id=match_id))
            return
        try:
            state = await self.ctx.hub.subscribe(self, match_id)
        except MatchNotFoundError:
            self.send_control(error_message("unknown match", match_id=match_id))
            return
        if self._closing:
            await self.ctx.hub.unsubscribe(self, match_id)
            return
        self.subscriptions.add(match_id)
        self.deliver(match_id, state.version, encode(snapshot_message(state)))

    async def _unsubscribe(self, match_id: str) -> None:
        if match_id in self.subscriptions:
            self.subscriptions.discard(match_id)
            await self.ctx.hub.unsubscribe(self, match_id)
            self.outbox.forget(match_id)
        self.send_control({"type": "unsubscribed", "match_id": match_id})

    async def _resync(self, match_id: str, reason: str = "resync") -> None:
        if match_id not in self.subscriptions:
            self.send_control(error_message("not subscribed", match_id=match_id))
            return
        state = await self.ctx.repository.get(match_id)
        # Unversioned so it is never skipped as a duplicate of what was sent.
        self.send_control(snapshot_message(state, reason=reason), key=match_id)

    # ---------------------------------------------------------------- outbound
    def deliver(self, match_id: str, version: int, payload: str) -> bool:
        return self.outbox.offer(match_id, version, payload)

    def send_control(self, message: dict[str, Any], key: str = CONTROL_KEY) -> None:
        if not self.outbox.offer(key, 0, encode(message)):
            self.evict("slow consumer")

    def evict(self, reason: str) -> None:
        if self._closing:
            return
        log.info("evicting client %s: %s", self.client_id, reason)
        if reason == "slow consumer":
            self.ctx.evicted_slow += 1
            self.close(CLOSE_SLOW_CONSUMER, reason)
        elif reason == "idle timeout":
            self.ctx.evicted_idle += 1
            self.close(CLOSE_IDLE_TIMEOUT, reason)
        else:
            self.close(CLOSE_WRITE_TIMEOUT, reason)

    async def _write_loop(self) -> None:
        timeout = self.ctx.settings.write_timeout
        while True:
            try:
                envelope = await self.outbox.get()
            except OutboxClosedError:
                return
            try:
                await asyncio.wait_for(self.write_message(envelope.payload), timeout)
            except WebSocketClosedError:
                return
            except TimeoutError:
                self.evict("write timeout")
                return
            self.outbox.mark_delivered()
            if not len(self.outbox):
                await self._resend_stale()

    async def _resend_stale(self) -> None:
        for match_id in self.outbox.take_stale():
            if match_id not in self.subscriptions:
                continue
            with contextlib.suppress(MatchNotFoundError):
                state = await self.ctx.repository.get(match_id)
                self.send_control(snapshot_message(state, reason="catch-up"), key=match_id)

    async def _heartbeat_loop(self) -> None:
        tick = min(self.heartbeat.interval / 4, 1.0)
        while not self._closing:
            await asyncio.sleep(tick)
            if self.heartbeat.expired():
                self.evict("idle timeout")
                return
            if self.heartbeat.should_ping():
                try:
                    self.ping(b"hb")
                except WebSocketClosedError:
                    return
                self.heartbeat.ping_sent()

    # ---------------------------------------------------------------- teardown
    def on_close(self) -> None:
        self._closing = True
        self.outbox.close()
        self.ctx.connections.discard(self)
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        self.subscriptions.clear()
        asyncio.ensure_future(self.ctx.hub.remove(self))

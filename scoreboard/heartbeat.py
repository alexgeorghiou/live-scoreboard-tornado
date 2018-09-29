"""Liveness tracking for a WebSocket connection.

The server sends a protocol-level ping every ``interval`` seconds. Any inbound
traffic (a pong, an application message, or an application-level ``ping``)
counts as proof of life. If nothing arrives for ``idle_timeout`` seconds the
connection is considered dead and closed, which reclaims sockets left behind by
clients that vanished without a FIN (mobile handovers, sleeping laptops).

The clock is injectable so the policy can be tested without sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class Heartbeat:
    def __init__(
        self,
        interval: float,
        idle_timeout: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval <= 0 or idle_timeout <= 0:
            raise ValueError("interval and idle_timeout must be positive")
        if idle_timeout < interval:
            raise ValueError("idle_timeout must be at least one ping interval")
        self.interval = interval
        self.idle_timeout = idle_timeout
        self._clock = clock
        self.last_seen = clock()
        self.last_ping = 0.0
        self.pings_sent = 0
        self.rtt: float | None = None

    def touch(self) -> None:
        self.last_seen = self._clock()

    def record_pong(self) -> None:
        now = self._clock()
        self.last_seen = now
        if self.last_ping:
            self.rtt = now - self.last_ping

    def idle_for(self) -> float:
        return self._clock() - self.last_seen

    def expired(self) -> bool:
        return self.idle_for() >= self.idle_timeout

    def should_ping(self) -> bool:
        return self._clock() - max(self.last_ping, self.last_seen) >= self.interval

    def ping_sent(self) -> None:
        self.last_ping = self._clock()
        self.pings_sent += 1

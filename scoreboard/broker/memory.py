"""In-process broker for tests and single-node development."""

from __future__ import annotations

from scoreboard.broker.base import Broker


class InMemoryBroker(Broker):
    """Delivers messages synchronously, in publish order, to local subscribers.

    Handlers are awaited inline, so a publish returns once every local
    subscriber has seen the message. The scoreboard's own handlers only enqueue
    work, which keeps this cheap and makes tests deterministic.
    """

    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))
        await self._dispatch(channel, message)

"""Shared base class for Tornado integration tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from tornado.httpclient import HTTPRequest
from tornado.testing import AsyncHTTPTestCase
from tornado.websocket import WebSocketClientConnection, websocket_connect

from scoreboard.app import make_app
from scoreboard.broker import InMemoryBroker
from scoreboard.config import Settings
from scoreboard.context import AppContext
from scoreboard.repository import InMemoryMatchRepository


class ScoreboardTestCase(AsyncHTTPTestCase):
    settings_overrides: dict[str, Any] = {}

    def make_context(self, settings: Settings) -> AppContext:
        return AppContext.from_parts(settings, InMemoryMatchRepository(), InMemoryBroker())

    def get_app(self):  # type: ignore[no-untyped-def]
        defaults: dict[str, Any] = {"ping_interval": 5.0, "idle_timeout": 15.0}
        settings = Settings(**{**defaults, **self.settings_overrides})
        self.ctx = self.make_context(settings)
        return make_app(self.ctx)

    async def api(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
        base_url: str | None = None,
    ) -> tuple[int, Any, Any]:
        payload = raw if raw is not None else (None if body is None else json.dumps(body))
        if payload is None and method in {"POST", "PUT"}:
            payload = b""
        url = (base_url + path) if base_url else self.get_url(path)
        response = await self.http_client.fetch(
            HTTPRequest(url, method=method, body=payload, headers=headers),
            raise_error=False,
        )
        data = json.loads(response.body) if response.body else None
        return response.code, data, response.headers

    async def create_match(self, match_id: str = "derby", kickoff: bool = True) -> None:
        code, _, _ = await self.api(
            "POST", "/api/matches", {"id": match_id, "home_team": "Rovers", "away_team": "United"}
        )
        assert code == 201
        if kickoff:
            await self.post_event(match_id, {"type": "period_change", "period": "first_half"})

    async def post_event(self, match_id: str, event: dict[str, Any]) -> dict[str, Any]:
        code, data, _ = await self.api("POST", f"/api/matches/{match_id}/events", event)
        assert code in (200, 201), data
        return data

    async def ws(self, query: str = "", url: str | None = None) -> WebSocketClientConnection:
        target = url or self.get_url("/ws").replace("http://", "ws://")
        conn = await websocket_connect(target + query)
        hello = await self.recv(conn)
        assert hello["type"] == "hello"
        return conn

    async def recv(self, conn: WebSocketClientConnection, timeout: float = 2.0) -> Any:
        message = await asyncio.wait_for(conn.read_message(), timeout)
        return None if message is None else json.loads(message)

    async def send(self, conn: WebSocketClientConnection, payload: dict[str, Any]) -> None:
        await conn.write_message(json.dumps(payload))

    async def recv_until(
        self, conn: WebSocketClientConnection, msg_type: str, timeout: float = 2.0
    ) -> dict[str, Any]:
        while True:
            message = await self.recv(conn, timeout)
            assert message is not None, f"connection closed waiting for {msg_type}"
            if message["type"] == msg_type:
                return message


GOAL = {"type": "goal", "team": "home", "player": "Silva", "minute": 12}

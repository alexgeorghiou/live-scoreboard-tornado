"""Two scoreboard instances sharing one (fake) Redis behave as one service."""

import fakeredis
from tornado.httpserver import HTTPServer
from tornado.testing import bind_unused_port, gen_test

from scoreboard.app import make_app
from scoreboard.broker.redis_broker import RedisBroker
from scoreboard.config import Settings
from scoreboard.context import AppContext
from scoreboard.repository.redis_repo import RedisMatchRepository
from tests.support import GOAL, ScoreboardTestCase


def redis_context(server: fakeredis.FakeServer, settings: Settings) -> AppContext:
    def client() -> fakeredis.FakeAsyncRedis:
        return fakeredis.FakeAsyncRedis(server=server, decode_responses=True)

    return AppContext.from_parts(
        settings,
        RedisMatchRepository(client(), prefix="it"),
        RedisBroker(client(), poll_timeout=0.05),
    )


class MultiInstanceTest(ScoreboardTestCase):
    def make_context(self, settings: Settings) -> AppContext:
        self.redis_server = fakeredis.FakeServer()
        self.settings = settings
        return redis_context(self.redis_server, settings)

    def tearDown(self) -> None:
        self.io_loop.run_sync(self.ctx.close)
        super().tearDown()

    @gen_test
    async def test_event_posted_on_one_instance_reaches_clients_on_another(self) -> None:
        other_ctx = redis_context(self.redis_server, self.settings)
        sock, port = bind_unused_port()
        other = HTTPServer(make_app(other_ctx))
        other.add_sockets([sock])
        try:
            await self.create_match()  # via instance A

            # Instance B sees the match through the shared repository...
            code, data, _ = await self.api(
                "GET", "/api/matches/derby", base_url=f"http://127.0.0.1:{port}"
            )
            assert (code, data["version"]) == (200, 1)

            # ...and its WebSocket clients receive events published by A.
            conn = await self.ws("?match=derby", url=f"ws://127.0.0.1:{port}/ws")
            assert (await self.recv(conn))["type"] == "snapshot"
            await self.post_event("derby", GOAL)
            update = await self.recv(conn)
            assert update["type"] == "event"
            assert update["state"]["score"]["home"] == 1
            conn.close()
        finally:
            other.stop()
            await other_ctx.close()

    @gen_test
    async def test_health_reports_redis(self) -> None:
        code, data, _ = await self.api("GET", "/healthz")
        assert (code, data["status"]) == (200, "ok")

import asyncio
from unittest import mock

from tornado.testing import gen_test

from scoreboard.web.socket import (
    CLOSE_IDLE_TIMEOUT,
    CLOSE_SLOW_CONSUMER,
    ScoreboardSocket,
)
from tests.support import GOAL, ScoreboardTestCase


class SubscriptionTest(ScoreboardTestCase):
    @gen_test
    async def test_hello_announces_heartbeat_settings(self) -> None:
        conn = await websocket_hello(self)
        assert conn["ping_interval"] == 5.0
        assert conn["idle_timeout"] == 15.0

    @gen_test
    async def test_subscribe_receives_snapshot_then_live_events(self) -> None:
        await self.create_match()
        conn = await self.ws()
        await self.send(conn, {"op": "subscribe", "match_id": "derby"})
        snapshot = await self.recv(conn)
        assert snapshot["type"] == "snapshot"
        assert snapshot["reason"] == "subscribe"
        assert snapshot["version"] == 1

        await self.post_event("derby", GOAL)
        update = await self.recv(conn)
        assert update["type"] == "event"
        assert update["version"] == 2
        assert update["event"]["type"] == "goal"
        assert update["state"]["score"] == {"home": 1, "away": 0}
        conn.close()

    @gen_test
    async def test_query_string_auto_subscribes(self) -> None:
        await self.create_match("a")
        await self.create_match("b")
        conn = await self.ws("?match=a,b")
        received = {(await self.recv(conn))["match_id"] for _ in range(2)}
        assert received == {"a", "b"}
        conn.close()

    @gen_test
    async def test_updates_only_reach_subscribers_of_that_match(self) -> None:
        await self.create_match("a")
        await self.create_match("b")
        on_a = await self.ws("?match=a")
        on_b = await self.ws("?match=b")
        await self.recv(on_a)
        await self.recv(on_b)

        await self.post_event("a", GOAL)
        assert (await self.recv(on_a))["match_id"] == "a"
        await self.send(on_b, {"op": "ping", "ts": 1})
        # The next frame for b is the pong, not a's goal.
        assert (await self.recv(on_b))["type"] == "pong"
        on_a.close()
        on_b.close()

    @gen_test
    async def test_many_clients_one_broker_subscription(self) -> None:
        await self.create_match()
        clients = [await self.ws("?match=derby") for _ in range(5)]
        for c in clients:
            await self.recv(c)
        assert self.ctx.hub.watchers("derby") == 5
        assert len(self.ctx.broker.channels) == 1
        await self.post_event("derby", GOAL)
        for c in clients:
            assert (await self.recv(c))["version"] == 2
        for c in clients:
            c.close()
        await wait_for(lambda: self.ctx.hub.watchers("derby") == 0)
        assert self.ctx.broker.channels == set()

    @gen_test
    async def test_unsubscribe_stops_updates(self) -> None:
        await self.create_match()
        conn = await self.ws("?match=derby")
        await self.recv(conn)
        await self.send(conn, {"op": "unsubscribe", "match_id": "derby"})
        assert await self.recv(conn) == {"type": "unsubscribed", "match_id": "derby"}
        await self.post_event("derby", GOAL)
        await self.send(conn, {"op": "ping", "ts": 2})
        assert (await self.recv(conn))["type"] == "pong"
        conn.close()

    @gen_test
    async def test_resync_sends_fresh_snapshot(self) -> None:
        await self.create_match()
        conn = await self.ws("?match=derby")
        await self.recv(conn)
        await self.send(conn, {"op": "resync", "match_id": "derby"})
        message = await self.recv(conn)
        assert (message["type"], message["reason"]) == ("snapshot", "resync")
        await self.send(conn, {"op": "resync", "match_id": "other"})
        assert (await self.recv(conn))["message"] == "not subscribed"
        conn.close()

    @gen_test
    async def test_snapshot_put_is_pushed_to_clients(self) -> None:
        await self.create_match()
        conn = await self.ws("?match=derby")
        await self.recv(conn)
        await self.api("PUT", "/api/matches/derby/snapshot", {"score": {"home": 4, "away": 4}})
        message = await self.recv(conn)
        assert message["type"] == "snapshot"
        assert message["state"]["score"] == {"home": 4, "away": 4}
        conn.close()


class ProtocolErrorTest(ScoreboardTestCase):
    settings_overrides = {"max_subscriptions": 1}

    @gen_test
    async def test_errors_are_reported_not_fatal(self) -> None:
        await self.create_match("a")
        await self.create_match("b")
        conn = await self.ws()
        await conn.write_message("not json")
        assert (await self.recv(conn))["type"] == "error"
        await self.send(conn, {"op": "dance"})
        assert "unknown op" in (await self.recv(conn))["message"]
        await self.send(conn, {"op": "subscribe"})
        assert "requires a match_id" in (await self.recv(conn))["message"]
        await self.send(conn, {"op": "subscribe", "match_id": "ghost"})
        assert (await self.recv(conn))["message"] == "unknown match"

        await self.send(conn, {"op": "subscribe", "match_id": "a"})
        assert (await self.recv(conn))["type"] == "snapshot"
        await self.send(conn, {"op": "subscribe", "match_id": "b"})
        assert (await self.recv(conn))["message"] == "subscription limit reached"
        # Re-subscribing to a match already followed just resends its snapshot.
        await self.send(conn, {"op": "subscribe", "match_id": "a"})
        assert (await self.recv(conn))["type"] == "snapshot"
        conn.close()


class HeartbeatTest(ScoreboardTestCase):
    settings_overrides = {"ping_interval": 0.05, "idle_timeout": 0.3}

    @gen_test
    async def test_application_ping_gets_pong(self) -> None:
        conn = await self.ws()
        await self.send(conn, {"op": "ping", "ts": 42})
        assert await self.recv(conn) == {"type": "pong", "ts": 42}
        conn.close()

    @gen_test
    async def test_responsive_client_survives_past_idle_timeout(self) -> None:
        # Tornado's client answers protocol pings automatically.
        conn = await self.ws()
        await asyncio.sleep(0.6)
        await self.send(conn, {"op": "ping", "ts": 1})
        assert (await self.recv(conn))["type"] == "pong"
        conn.close()

    @gen_test
    async def test_silent_client_is_closed_after_idle_timeout(self) -> None:
        with mock.patch.object(ScoreboardSocket, "on_pong", lambda self, data: None):
            conn = await self.ws()
            assert await self.recv(conn, timeout=2.0) is None
        assert conn.close_code == CLOSE_IDLE_TIMEOUT
        assert self.ctx.evicted_idle == 1


class DisconnectSlowConsumerTest(ScoreboardTestCase):
    settings_overrides = {"outbox_size": 2, "overflow_policy": "disconnect"}

    @gen_test
    async def test_burst_overflow_disconnects_client(self) -> None:
        await self.create_match()
        conn = await self.ws("?match=derby")
        await self.recv(conn)
        # Publish a burst without yielding to the loop: the writer cannot
        # drain, so the bounded outbox overflows.
        for minute in range(1, 10):
            await self.ctx.service.post_event("derby", {**GOAL, "minute": minute})
        while (await self.recv(conn)) is not None:
            pass
        assert conn.close_code == CLOSE_SLOW_CONSUMER
        assert self.ctx.evicted_slow == 1
        assert self.ctx.hub.stats.evictions == 1


class DropOldestTest(ScoreboardTestCase):
    settings_overrides = {"outbox_size": 2, "overflow_policy": "drop_oldest"}

    @gen_test
    async def test_burst_is_conflated_and_client_converges(self) -> None:
        await self.create_match()
        conn = await self.ws("?match=derby")
        await self.recv(conn)
        for minute in range(1, 21):
            await self.ctx.service.post_event("derby", {**GOAL, "minute": minute})

        seen = []
        while True:
            message = await self.recv(conn)
            seen.append(message["version"])
            if message["state"]["score"]["home"] == 20:
                break
        assert len(seen) < 20
        assert seen == sorted(seen)
        assert seen[-1] == 21
        socket = next(iter(self.ctx.connections))
        assert socket.outbox.stats.conflated > 0
        conn.close()


async def websocket_hello(case: ScoreboardTestCase) -> dict:
    from tornado.websocket import websocket_connect

    conn = await websocket_connect(case.get_url("/ws").replace("http://", "ws://"))
    hello = await case.recv(conn)
    conn.close()
    return hello


async def wait_for(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)

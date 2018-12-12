"""Tornado application factory and process entry point."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

import tornado.web

from scoreboard.config import Settings
from scoreboard.context import AppContext
from scoreboard.web.api import (
    EventsHandler,
    HealthHandler,
    MatchesHandler,
    MatchHandler,
    SnapshotHandler,
    StatsHandler,
)
from scoreboard.web.socket import ScoreboardSocket

log = logging.getLogger("scoreboard")

STATIC_DIR = Path(
    os.environ.get("SCOREBOARD_STATIC_DIR") or Path(__file__).resolve().parent.parent / "static"
)
MATCH_ID = r"(?P<match_id>[a-z0-9][a-z0-9-]{0,63})"


class IndexHandler(tornado.web.StaticFileHandler):
    def set_extra_headers(self, path: str) -> None:
        self.set_header("Cache-Control", "no-cache")


def make_app(ctx: AppContext, static_dir: Path = STATIC_DIR) -> tornado.web.Application:
    deps = {"ctx": ctx}
    routes = [
        (r"/healthz", HealthHandler, deps),
        (r"/api/stats", StatsHandler, deps),
        (r"/api/matches", MatchesHandler, deps),
        (rf"/api/matches/{MATCH_ID}", MatchHandler, deps),
        (rf"/api/matches/{MATCH_ID}/events", EventsHandler, deps),
        (rf"/api/matches/{MATCH_ID}/snapshot", SnapshotHandler, deps),
        (r"/ws", ScoreboardSocket, deps),
        (
            r"/(.*)",
            IndexHandler,
            {"path": str(static_dir), "default_filename": "index.html"},
        ),
    ]
    return tornado.web.Application(
        routes,
        debug=ctx.settings.debug,
        websocket_max_message_size=ctx.settings.max_message_size,
        compress_response=True,
    )


async def serve(settings: Settings) -> None:
    ctx = AppContext.build(settings)
    app = make_app(ctx)
    server = app.listen(settings.port, settings.host, xheaders=True)
    backend = "redis" if settings.redis_url else "in-memory"
    log.info("scoreboard listening on %s:%d (%s backend)", settings.host, settings.port, backend)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not available on Windows event loops; Ctrl+C still raises KeyboardInterrupt.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        server.stop()
        await ctx.close()
        await server.close_all_connections()


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(settings))


if __name__ == "__main__":
    main()

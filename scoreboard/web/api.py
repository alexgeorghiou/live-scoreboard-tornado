"""REST API for feeds and operators: create matches, post events, push snapshots."""

from __future__ import annotations

import hmac
import json
import time
from http import HTTPStatus
from typing import Any

from tornado.web import HTTPError, RequestHandler

from scoreboard import __version__
from scoreboard.context import AppContext
from scoreboard.domain.events import ValidationError
from scoreboard.domain.state import IllegalEventError, MatchState
from scoreboard.repository import MatchExistsError, MatchNotFoundError, VersionConflictError


class ApiError(HTTPError):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(status, reason=HTTPStatus(status).phrase)
        self.code = code
        self.message = message
        self.extra = extra


class BaseHandler(RequestHandler):
    ctx: AppContext
    write_methods = frozenset({"POST", "PUT", "DELETE", "PATCH"})

    def initialize(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def set_default_headers(self) -> None:
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.set_header("Cache-Control", "no-store")

    def prepare(self) -> None:
        token = self.ctx.settings.api_token
        if token and self.request.method in self.write_methods:
            header = self.request.headers.get("Authorization", "")
            supplied = header.removeprefix("Bearer ").strip()
            if not header.startswith("Bearer ") or not hmac.compare_digest(supplied, token):
                raise ApiError(401, "unauthorized", "a valid bearer token is required")

    def json_body(self) -> Any:
        try:
            return json.loads(self.request.body or b"null")
        except ValueError:
            raise ApiError(400, "invalid_json", "request body is not valid JSON") from None

    def send_json(self, payload: Any, status: int = 200) -> None:
        self.set_status(status)
        self.finish(json.dumps(payload, separators=(",", ":")))

    def send_state(self, state: MatchState, status: int = 200) -> None:
        self.set_header("ETag", f'"{state.version}"')
        self.send_json(state.to_dict(), status)

    def log_exception(self, typ, value, tb) -> None:  # type: ignore[no-untyped-def]
        if isinstance(value, ApiError | ValidationError | IllegalEventError | LookupError):
            return
        if isinstance(value, MatchExistsError | VersionConflictError):
            return
        super().log_exception(typ, value, tb)

    def _handle_request_exception(self, e: BaseException) -> None:
        mapped = _map_exception(e)
        if mapped is None:
            super()._handle_request_exception(e)
            return
        if self._finished:
            return
        self.log_exception(type(e), e, e.__traceback__)
        self.clear()
        self.set_default_headers()
        body = {"error": mapped.code, "message": mapped.message, **mapped.extra}
        self.send_json(body, mapped.status_code)

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        self.finish(json.dumps({"error": "http_error", "message": self._reason}))


def _map_exception(e: BaseException) -> ApiError | None:
    if isinstance(e, ApiError):
        return e
    if isinstance(e, ValidationError):
        return ApiError(400, "validation_error", str(e))
    if isinstance(e, MatchNotFoundError):
        return ApiError(404, "not_found", str(e))
    if isinstance(e, MatchExistsError):
        return ApiError(409, "conflict", str(e))
    if isinstance(e, IllegalEventError):
        return ApiError(409, "illegal_event", str(e))
    if isinstance(e, VersionConflictError):
        return ApiError(412, "version_conflict", str(e), current_version=e.actual)
    return None


def _expected_version(handler: RequestHandler) -> int | None:
    raw = handler.request.headers.get("If-Match")
    if raw is None:
        return None
    try:
        return int(raw.strip().strip('"'))
    except ValueError:
        raise ApiError(400, "invalid_header", "If-Match must be a version number") from None


class MatchesHandler(BaseHandler):
    async def get(self) -> None:
        states = sorted(await self.ctx.repository.list(), key=lambda s: s.match_id)
        self.send_json(
            {
                "matches": [
                    {
                        "match_id": s.match_id,
                        "home_team": s.home_team,
                        "away_team": s.away_team,
                        "competition": s.competition,
                        "status": s.status,
                        "period": s.period.value,
                        "minute": s.minute,
                        "score": {"home": s.score.home, "away": s.score.away},
                        "version": s.version,
                        "watchers": self.ctx.hub.watchers(s.match_id),
                    }
                    for s in states
                ]
            }
        )

    async def post(self) -> None:
        state = await self.ctx.service.create_match(self.json_body())
        self.set_header("Location", f"/api/matches/{state.match_id}")
        self.send_state(state, 201)


class MatchHandler(BaseHandler):
    async def get(self, match_id: str) -> None:
        self.send_state(await self.ctx.repository.get(match_id))


class EventsHandler(BaseHandler):
    async def get(self, match_id: str) -> None:
        try:
            since = int(self.get_query_argument("since", "0"))
        except ValueError:
            raise ApiError(400, "invalid_query", "'since' must be an integer") from None
        events = await self.ctx.repository.events(match_id, since)
        self.send_json({"match_id": match_id, "events": [e.to_dict() for e in events]})

    async def post(self, match_id: str) -> None:
        result = await self.ctx.service.post_event(
            match_id, self.json_body(), _expected_version(self)
        )
        self.set_header("ETag", f'"{result.state.version}"')
        self.send_json(
            {
                "duplicate": result.duplicate,
                "event": result.event.to_dict(),
                "state": result.state.to_dict(),
            },
            200 if result.duplicate else 201,
        )


class SnapshotHandler(BaseHandler):
    async def put(self, match_id: str) -> None:
        state = await self.ctx.service.put_snapshot(match_id, self.json_body())
        self.send_state(state)


class HealthHandler(BaseHandler):
    async def get(self) -> None:
        broker_ok = True
        ping = getattr(self.ctx.broker, "ping", None)
        if ping is not None:
            broker_ok = await ping()
        self.send_json(
            {"status": "ok" if broker_ok else "degraded", "version": __version__},
            200 if broker_ok else 503,
        )


class StatsHandler(BaseHandler):
    async def get(self) -> None:
        hub = self.ctx.hub
        conns = list(self.ctx.connections)
        self.send_json(
            {
                "uptime_seconds": round(time.time() - self.ctx.started_at, 1),
                "connections": len(conns),
                "subscribed_matches": hub.matches,
                "hub": {
                    "messages_in": hub.stats.messages_in,
                    "deliveries": hub.stats.deliveries,
                    "evictions": hub.stats.evictions,
                    "malformed": hub.stats.malformed,
                },
                "evicted": {"slow": self.ctx.evicted_slow, "idle": self.ctx.evicted_idle},
                "outbox": {
                    "queued": sum(len(c.outbox) for c in conns),
                    "dropped": sum(c.outbox.stats.dropped for c in conns),
                    "conflated": sum(c.outbox.stats.conflated for c in conns),
                },
            }
        )

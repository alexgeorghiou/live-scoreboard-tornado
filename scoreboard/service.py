"""Application service: validates commands, persists them, publishes updates."""

from __future__ import annotations

import re
import uuid
from typing import Any

from scoreboard.broker import Broker, match_channel
from scoreboard.domain.events import ValidationError, parse_event
from scoreboard.domain.state import MatchState
from scoreboard.messages import encode, event_message, snapshot_message
from scoreboard.repository import AppendResult, MatchRepository

MATCH_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_match_id(match_id: str) -> str:
    if not MATCH_ID_RE.match(match_id):
        raise ValidationError("match id must be 1-64 chars of lowercase letters, digits or '-'")
    return match_id


class ScoreboardService:
    def __init__(self, repository: MatchRepository, broker: Broker) -> None:
        self.repository = repository
        self.broker = broker

    async def create_match(self, payload: Any) -> MatchState:
        if not isinstance(payload, dict):
            raise ValidationError("match payload must be a JSON object")
        match_id = validate_match_id(str(payload.get("id") or uuid.uuid4().hex[:12]))
        teams = {}
        for key in ("home_team", "away_team"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"'{key}' must be a non-empty string")
            teams[key] = value.strip()
        if teams["home_team"].casefold() == teams["away_team"].casefold():
            raise ValidationError("a team cannot play itself")
        state = MatchState(
            match_id=match_id,
            competition=str(payload.get("competition", "")).strip(),
            **teams,
        )
        return await self.repository.create(state)

    async def post_event(
        self, match_id: str, payload: Any, expected_version: int | None = None
    ) -> AppendResult:
        event = parse_event(payload)
        result = await self.repository.append(match_id, event, expected_version)
        if not result.duplicate:
            await self.broker.publish(
                match_channel(match_id), encode(event_message(result.event, result.state))
            )
        return result

    async def put_snapshot(self, match_id: str, payload: Any) -> MatchState:
        if not isinstance(payload, dict):
            raise ValidationError("snapshot payload must be a JSON object")
        current = await self.repository.get(match_id)
        merged = {
            **current.to_dict(),
            **payload,
            "match_id": match_id,
            "version": current.version,
        }
        try:
            snapshot = MatchState.from_dict(merged)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        installed = await self.repository.replace(snapshot)
        await self.broker.publish(
            match_channel(match_id), encode(snapshot_message(installed, reason="resync"))
        )
        return installed

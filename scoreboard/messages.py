"""Wire format shared by the broker and the WebSocket protocol.

Every update carries the full derived state alongside the triggering event.
That costs a few hundred bytes per message but means any single message is
enough to render the scoreboard, so a client that misses updates (because it
was too slow and some were dropped) self-heals on the next one.
"""

from __future__ import annotations

import json
from typing import Any

from scoreboard.domain.events import MatchEvent
from scoreboard.domain.state import MatchState


def encode(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"))


def event_message(event: MatchEvent, state: MatchState) -> dict[str, Any]:
    return {
        "type": "event",
        "match_id": state.match_id,
        "version": state.version,
        "event": event.to_dict(),
        "state": state.to_dict(),
    }


def snapshot_message(state: MatchState, reason: str = "subscribe") -> dict[str, Any]:
    return {
        "type": "snapshot",
        "match_id": state.match_id,
        "version": state.version,
        "reason": reason,
        "state": state.to_dict(),
    }


def error_message(message: str, **extra: Any) -> dict[str, Any]:
    return {"type": "error", "message": message, **extra}

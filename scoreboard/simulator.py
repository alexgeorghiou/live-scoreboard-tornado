"""Demo feed: plays out random matches against the REST API.

    python -m scoreboard.simulator --url http://localhost:8888 --matches 3 --tick 1.0

Each tick is one match minute. :class:`MatchScript` generates the events and
tracks just enough state (who is on the pitch, who is booked) to only produce
legal events; the server's reducer remains the authority.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from tornado.httpclient import AsyncHTTPClient, HTTPClientError

log = logging.getLogger("scoreboard.simulator")

TEAMS = [
    "Harbour Athletic", "Northgate Rovers", "Riverside United", "Kingsbridge Town",
    "Old Mill FC", "Westfield Wanderers", "Castle Hill", "Lakeside Albion",
]  # fmt: skip
SURNAMES = [
    "Silva", "Okafor", "Ito", "Novak", "Hughes", "Moreau", "Kowalski", "Diaz", "Larsen",
    "Bianchi", "Mensah", "Petrov", "Costa", "Byrne", "Sato", "Fischer", "Ahmed", "Rossi",
]  # fmt: skip


@dataclass
class Squad:
    starters: list[str]
    bench: list[str]
    booked: set[str] = field(default_factory=set)
    subs_used: int = 0

    @classmethod
    def random(cls, rng: random.Random) -> Squad:
        names = rng.sample(SURNAMES, 16)
        return cls(starters=names[:11], bench=names[11:])


@dataclass
class MatchScript:
    rng: random.Random
    home: Squad
    away: Squad
    score: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})

    @classmethod
    def create(cls, seed: int | None = None) -> MatchScript:
        rng = random.Random(seed)
        return cls(rng, Squad.random(rng), Squad.random(rng))

    def squad(self, team: str) -> Squad:
        return self.home if team == "home" else self.away

    def events(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """Yield ``(minute, payload)`` pairs for a full match."""
        yield 0, {"type": "period_change", "period": "first_half", "minute": 0}
        for minute in range(1, 46):
            yield from self._minute(minute)
        yield 45, {"type": "period_change", "period": "half_time", "minute": 45}
        yield 46, {"type": "period_change", "period": "second_half", "minute": 46}
        for minute in range(46, 91):
            yield from self._minute(minute)
        yield 90, {"type": "period_change", "period": "full_time", "minute": 90}

    def _minute(self, minute: int) -> Iterator[tuple[int, dict[str, Any]]]:
        roll = self.rng.random()
        team = self.rng.choice(["home", "away"])
        squad = self.squad(team)
        if not squad.starters:
            return
        if roll < 0.03:
            scorer = self.rng.choice(squad.starters)
            self.score[team] += 1
            yield (
                minute,
                {
                    "type": "goal",
                    "team": team,
                    "player": scorer,
                    "minute": minute,
                    "penalty": self.rng.random() < 0.1,
                },
            )
        elif roll < 0.05:
            player = self.rng.choice(squad.starters)
            straight_red = self.rng.random() < 0.05
            if straight_red or player in squad.booked:
                squad.starters.remove(player)
            else:
                squad.booked.add(player)
            yield (
                minute,
                {
                    "type": "card",
                    "team": team,
                    "player": player,
                    "card": "red" if straight_red else "yellow",
                    "minute": minute,
                },
            )
        elif minute > 55 and roll < 0.08 and squad.bench and squad.subs_used < 5:
            off = self.rng.choice(squad.starters)
            on = squad.bench.pop(0)
            squad.starters.remove(off)
            squad.starters.append(on)
            squad.subs_used += 1
            yield (
                minute,
                {
                    "type": "substitution",
                    "team": team,
                    "player_off": off,
                    "player_on": on,
                    "minute": minute,
                },
            )


async def _request(
    client: AsyncHTTPClient, method: str, url: str, body: Any, token: str
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = await client.fetch(url, method=method, body=json.dumps(body), headers=headers)
    return json.loads(response.body)


async def run_match(
    base_url: str, match_id: str, tick: float, token: str, seed: int | None = None
) -> None:
    client = AsyncHTTPClient()
    rng = random.Random(seed)
    home, away = rng.sample(TEAMS, 2)
    try:
        await _request(
            client,
            "POST",
            f"{base_url}/api/matches",
            {"id": match_id, "home_team": home, "away_team": away, "competition": "Demo League"},
            token,
        )
    except HTTPClientError as exc:
        if exc.code != 409:
            raise
        log.info("match %s already exists; replaying events anyway", match_id)

    script = MatchScript.create(seed)
    last_minute = 0
    for minute, payload in script.events():
        if minute > last_minute:
            await asyncio.sleep(tick * (minute - last_minute))
            last_minute = minute
        try:
            await _request(
                client, "POST", f"{base_url}/api/matches/{match_id}/events", payload, token
            )
        except HTTPClientError as exc:
            body = exc.response.body.decode() if exc.response else ""
            log.warning("%s rejected %s: %s", match_id, payload["type"], body)
    log.info("%s finished %s", match_id, script.score)


async def main_async(args: argparse.Namespace) -> None:
    await asyncio.gather(
        *(
            run_match(args.url.rstrip("/"), f"{args.prefix}-{i + 1}", args.tick, args.token, i)
            for i in range(args.matches)
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8888")
    parser.add_argument("--matches", type=int, default=3)
    parser.add_argument("--tick", type=float, default=1.0, help="seconds per match minute")
    parser.add_argument("--prefix", default="demo")
    parser.add_argument("--token", default="")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()

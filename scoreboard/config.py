"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from scoreboard.backpressure import OverflowPolicy


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    return default if raw in (None, "") else int(raw)


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    return default if raw in (None, "") else float(raw)


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8888
    # Empty means "use the in-memory broker and repository".
    redis_url: str = ""
    redis_prefix: str = "scoreboard"
    # Bearer token required for write endpoints; empty disables auth (dev only).
    api_token: str = ""
    ping_interval: float = 15.0
    idle_timeout: float = 45.0
    write_timeout: float = 10.0
    outbox_size: int = 64
    overflow_policy: OverflowPolicy = OverflowPolicy.DROP_OLDEST
    max_drops: int = 256
    max_subscriptions: int = 20
    max_message_size: int = 4096
    allowed_origins: tuple[str, ...] = field(default_factory=tuple)
    debug: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        origins = tuple(o.strip() for o in env.get("ALLOWED_ORIGINS", "").split(",") if o.strip())
        return cls(
            host=env.get("HOST", cls.host),
            port=_int(env, "PORT", cls.port),
            redis_url=env.get("REDIS_URL", ""),
            redis_prefix=env.get("REDIS_PREFIX", cls.redis_prefix),
            api_token=env.get("API_TOKEN", ""),
            ping_interval=_float(env, "PING_INTERVAL", cls.ping_interval),
            idle_timeout=_float(env, "IDLE_TIMEOUT", cls.idle_timeout),
            write_timeout=_float(env, "WRITE_TIMEOUT", cls.write_timeout),
            outbox_size=_int(env, "OUTBOX_SIZE", cls.outbox_size),
            overflow_policy=OverflowPolicy(env.get("OVERFLOW_POLICY", cls.overflow_policy)),
            max_drops=_int(env, "MAX_DROPS", cls.max_drops),
            max_subscriptions=_int(env, "MAX_SUBSCRIPTIONS", cls.max_subscriptions),
            max_message_size=_int(env, "MAX_MESSAGE_SIZE", cls.max_message_size),
            allowed_origins=origins,
            debug=env.get("DEBUG", "").lower() in {"1", "true", "yes"},
        )

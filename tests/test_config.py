import pytest

from scoreboard.backpressure import OverflowPolicy
from scoreboard.config import Settings


def test_defaults_from_empty_environment() -> None:
    settings = Settings.from_env({})
    assert settings.port == 8888
    assert settings.redis_url == ""
    assert settings.overflow_policy is OverflowPolicy.DROP_OLDEST
    assert settings.allowed_origins == ()


def test_reads_overrides() -> None:
    settings = Settings.from_env(
        {
            "PORT": "9000",
            "REDIS_URL": "redis://redis:6379/0",
            "PING_INTERVAL": "2.5",
            "OVERFLOW_POLICY": "disconnect",
            "ALLOWED_ORIGINS": "https://a.example, https://b.example",
            "DEBUG": "true",
        }
    )
    assert settings.port == 9000
    assert settings.ping_interval == 2.5
    assert settings.overflow_policy is OverflowPolicy.DISCONNECT
    assert settings.allowed_origins == ("https://a.example", "https://b.example")
    assert settings.debug


def test_invalid_policy_is_rejected() -> None:
    with pytest.raises(ValueError):
        Settings.from_env({"OVERFLOW_POLICY": "ignore"})

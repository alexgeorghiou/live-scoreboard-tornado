import pytest

from scoreboard.heartbeat import Heartbeat


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_validates_configuration() -> None:
    with pytest.raises(ValueError):
        Heartbeat(0, 10)
    with pytest.raises(ValueError):
        Heartbeat(10, 5)


def test_ping_schedule_and_expiry() -> None:
    clock = FakeClock()
    hb = Heartbeat(interval=10, idle_timeout=25, clock=clock)
    assert not hb.should_ping()
    clock.now += 10
    assert hb.should_ping()
    hb.ping_sent()
    assert not hb.should_ping()
    clock.now += 10
    assert hb.should_ping() and not hb.expired()
    clock.now += 5
    assert hb.expired()
    assert hb.pings_sent == 1


def test_inbound_traffic_keeps_connection_alive() -> None:
    clock = FakeClock()
    hb = Heartbeat(interval=10, idle_timeout=25, clock=clock)
    for _ in range(5):
        clock.now += 9
        hb.touch()
        assert not hb.expired()
        # A chatty client does not need to be pinged.
        assert not hb.should_ping()


def test_pong_records_round_trip_time() -> None:
    clock = FakeClock()
    hb = Heartbeat(interval=1, idle_timeout=5, clock=clock)
    clock.now += 1
    hb.ping_sent()
    clock.now += 0.25
    hb.record_pong()
    assert hb.rtt == pytest.approx(0.25)
    assert hb.idle_for() == 0

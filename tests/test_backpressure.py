import asyncio

import pytest

from scoreboard.backpressure import Outbox, OutboxClosedError, OverflowPolicy


def drain(box: Outbox) -> list[tuple[str, int]]:
    out = []
    while len(box):
        item = box._items.popleft()
        out.append((item.key, item.version))
    return out


def test_rejects_non_positive_size() -> None:
    with pytest.raises(ValueError):
        Outbox(maxsize=0)


async def test_fifo_delivery() -> None:
    box = Outbox(maxsize=4)
    for v in (1, 2, 3):
        assert box.offer("a", v, f"a{v}")
    assert [(await box.get()).payload for _ in range(3)] == ["a1", "a2", "a3"]


async def test_get_waits_for_items_and_close_wakes_reader() -> None:
    box = Outbox()
    waiter = asyncio.create_task(box.get())
    await asyncio.sleep(0)
    box.offer("a", 1, "x")
    assert (await waiter).payload == "x"
    waiter = asyncio.create_task(box.get())
    await asyncio.sleep(0)
    box.close()
    with pytest.raises(OutboxClosedError):
        await waiter
    assert not box.offer("a", 2, "y")


def test_older_versions_are_skipped() -> None:
    box = Outbox()
    box.offer("a", 5, "v5")
    box.offer("a", 4, "v4")
    box.offer("a", 5, "v5-again")
    box.offer("b", 1, "b1")
    assert drain(box) == [("a", 5), ("b", 1)]
    assert box.stats.skipped_stale_versions == 2


def test_unversioned_messages_are_never_skipped() -> None:
    box = Outbox()
    box.offer("ctl", 0, "pong")
    box.offer("ctl", 0, "pong")
    assert len(box) == 2


def test_disconnect_policy_closes_when_full() -> None:
    box = Outbox(maxsize=2, policy=OverflowPolicy.DISCONNECT)
    assert box.offer("a", 1, "1")
    assert box.offer("a", 2, "2")
    assert not box.offer("a", 3, "3")
    assert box.closed and box.overflowed


def test_drop_oldest_conflates_same_match_first() -> None:
    box = Outbox(maxsize=3)
    box.offer("a", 1, "a1")
    box.offer("b", 1, "b1")
    box.offer("a", 2, "a2")
    # Full; incoming b2 makes b1 redundant, but a1 is older and also superseded.
    assert box.offer("b", 2, "b2")
    assert drain(box) == [("b", 1), ("a", 2), ("b", 2)]
    assert box.stats.conflated == 1
    assert box.take_stale() == set()


def test_drop_oldest_marks_match_stale_when_nothing_to_conflate() -> None:
    box = Outbox(maxsize=2)
    box.offer("a", 1, "a1")
    box.offer("b", 1, "b1")
    assert box.offer("c", 1, "c1")
    assert drain(box) == [("b", 1), ("c", 1)]
    assert box.stats.dropped == 1
    assert box.take_stale() == {"a"}
    assert box.take_stale() == set()


def test_persistent_slow_consumer_is_disconnected() -> None:
    box = Outbox(maxsize=1, max_drops=3)
    box.offer("a", 1, "x")
    for v in range(2, 5):
        assert box.offer("a", v, "x")
    assert not box.offer("a", 5, "x")
    assert box.overflowed


def test_delivery_resets_drop_budget() -> None:
    box = Outbox(maxsize=1, max_drops=1)
    box.offer("a", 1, "x")
    assert box.offer("a", 2, "x")
    box.mark_delivered()
    assert box.offer("a", 3, "x")
    assert not box.closed


def test_forget_removes_queued_messages_and_versions() -> None:
    box = Outbox(maxsize=2)
    box.offer("a", 3, "a3")
    box.offer("b", 1, "b1")
    box.offer("c", 1, "c1")  # drops a3 -> a stale
    box.forget("a")
    assert box.take_stale() == set()
    assert box.offer("a", 1, "a1")  # version history cleared

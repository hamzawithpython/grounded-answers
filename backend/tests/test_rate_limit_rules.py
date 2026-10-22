"""Rate-limit rules that need no Valkey (ADR 0014): delays, windows, keys, and the timing floor."""

import asyncio
from typing import Any

import pytest

from grounded_answers.rate_limit import (
    ACCOUNT_DELAY_CAP_MS,
    IP_WINDOW_MS,
    account_delay_ms,
    digest,
    retry_seconds,
    window_end_ms,
    window_index,
)
from grounded_answers.timing import TimingFloor


def test_progressive_delay_is_one_two_four_seconds_then_capped() -> None:
    assert [account_delay_ms(n) for n in range(1, 5)] == [0, 0, 0, 0]
    assert account_delay_ms(5) == 1000
    assert account_delay_ms(6) == 2000
    assert account_delay_ms(7) == 4000
    assert account_delay_ms(8) == 8000
    assert account_delay_ms(9) == 16000
    assert account_delay_ms(10) == ACCOUNT_DELAY_CAP_MS == 30000
    assert account_delay_ms(60) == 30000


def test_window_index_moves_on_at_the_boundary() -> None:
    assert window_index(IP_WINDOW_MS - 1, IP_WINDOW_MS) == 0
    assert window_index(IP_WINDOW_MS, IP_WINDOW_MS) == 1
    assert window_end_ms(IP_WINDOW_MS - 1, IP_WINDOW_MS) == IP_WINDOW_MS


def test_retry_after_rounds_up_and_is_never_zero() -> None:
    assert retry_seconds(1) == 1
    assert retry_seconds(1000) == 1
    assert retry_seconds(1001) == 2
    assert retry_seconds(0) == 1


def test_digest_hides_the_value_and_separates_namespaces() -> None:
    key = b"x" * 32
    email = "hr.manager@indusfreight.example"
    value = digest(key, "account", email)
    assert email not in value
    assert "indusfreight" not in value
    assert len(value) == 64
    assert digest(key, "account", email) == value, "the same input gives the same key"
    assert digest(key, "ip", email) != value, "a different namespace gives a different key"
    assert digest(b"y" * 32, "account", email) != value, "a different secret gives a different key"


@pytest.mark.anyio
async def test_timing_floor_waits_the_remainder_and_never_shortens() -> None:
    now = [100.0]
    slept: list[float] = []

    def clock() -> float:
        return now[0]

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    floor = TimingFloor(floor_seconds=0.5, clock=clock, sleep=fake_sleep)
    started = floor.start()
    now[0] += 0.2
    await floor.hold(started)
    assert slept == [pytest.approx(0.3)]
    assert now[0] == pytest.approx(100.5)

    slept.clear()
    started = floor.start()
    now[0] += 0.9  # slower than the floor: no extra wait
    await floor.hold(started)
    assert slept == []


@pytest.mark.anyio
async def test_known_and_unknown_paths_end_at_the_same_floor() -> None:
    """Both paths call hold() with their own start. With a fake clock, each ends exactly at the floor."""
    results: list[float] = []

    async def run_path(work_seconds: float) -> None:
        now = [0.0]
        slept: list[float] = []

        def clock() -> float:
            return now[0]

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        floor = TimingFloor(floor_seconds=0.5, clock=clock, sleep=fake_sleep)
        started = floor.start()
        now[0] += work_seconds  # the fake work takes this long
        await floor.hold(started)
        results.append(now[0])

    await asyncio.gather(run_path(0.05), run_path(0.1))
    assert results == [pytest.approx(0.5), pytest.approx(0.5)]


def test_limits_are_read_from_the_module_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented numbers are the ones the code uses (ADR 0014)."""
    import grounded_answers.rate_limit as rl

    values: dict[str, Any] = {
        "PAIR_LIMIT": 5,
        "IP_LIMIT": 20,
        "ACCOUNT_DELAY_START": 5,
        "HARD_LIMIT": 50,
        "REFRESH_LIMIT": 300,
    }
    for name, expected in values.items():
        assert getattr(rl, name) == expected, name
    assert rl.PAIR_WINDOW_MS == rl.PAIR_LOCK_MS == 15 * 60 * 1000
    assert rl.HARD_LOCK_MS == 24 * 60 * 60 * 1000

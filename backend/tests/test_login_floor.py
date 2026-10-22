"""The login timing floor is a bounded setting, and an overrun is logged without personal data (ADR 0014)."""

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from grounded_answers.config import Settings
from grounded_answers.timing import TimingFloor


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+psycopg://app:x@127.0.0.1:5432/db",
        "redis_url": "redis://127.0.0.1:6379/0",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_floor_default_is_500_milliseconds() -> None:
    assert _settings().auth_login_floor_ms == 500


@pytest.mark.parametrize("value", [99, 5001, 0, -1])
def test_floor_outside_the_bounds_is_refused(value: int) -> None:
    with pytest.raises(ValidationError):
        _settings(auth_login_floor_ms=value)


@pytest.mark.parametrize("value", [100, 5000])
def test_floor_at_the_bounds_is_accepted(value: int) -> None:
    assert _settings(auth_login_floor_ms=value).auth_login_floor_ms == value


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.mark.anyio
async def test_overrun_logs_a_warning_with_numbers_only(caplog: pytest.LogCaptureFixture) -> None:
    clock = _Clock()
    floor = TimingFloor(floor_seconds=0.5, clock=clock, sleep=_no_sleep)
    started = floor.start()
    clock.now += 0.8  # the real work took 800 ms
    with caplog.at_level(logging.WARNING, logger="grounded_answers.auth"):
        await floor.hold(started)
    assert "login.timing_floor_exceeded floor_ms=500 work_ms=800" in caplog.text
    for personal in ("@", "ip", "203.0.113", "password"):
        assert personal not in caplog.text


@pytest.mark.anyio
async def test_work_under_the_floor_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    clock = _Clock()
    floor = TimingFloor(floor_seconds=0.5, clock=clock, sleep=_no_sleep)
    started = floor.start()
    clock.now += 0.2
    with caplog.at_level(logging.WARNING, logger="grounded_answers.auth"):
        await floor.hold(started)
    assert "login.timing_floor_exceeded" not in caplog.text


@pytest.mark.anyio
async def test_a_configured_floor_reaches_the_timing_object() -> None:
    """The lifespan builds TimingFloor from the setting: milliseconds become seconds."""
    settings = _settings(auth_login_floor_ms=750)
    floor = TimingFloor(floor_seconds=settings.auth_login_floor_ms / 1000)
    assert floor.floor_seconds == 0.75

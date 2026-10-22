"""
A fixed minimum duration for login responses (ADR 0014, R11).

A known user's failed login writes an audit row, and an unknown email does not. A precise timer
could tell the two apart. Every login response therefore waits until a fixed floor has passed since
the request started. The floor is set above the measured worst case of the slowest path (ADR 0014
records the measurement), so all paths end at the same time.

The wait uses asyncio.sleep, so it does not hold a thread or use CPU. The clock and the sleep are
parameters, so the tests can drive time without waiting.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger("grounded_answers.auth")

Monotonic = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

LOGIN_FLOOR_SECONDS = 0.5


class TimingFloor:
    def __init__(
        self,
        floor_seconds: float = LOGIN_FLOOR_SECONDS,
        clock: Monotonic = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.floor_seconds = floor_seconds
        self.clock = clock
        self.sleep = sleep

    def start(self) -> float:
        return self.clock()

    async def hold(self, started: float) -> None:
        """
        Wait until floor_seconds have passed since `started`. Never shortens a slow response.

        If the real work took longer than the floor, the floor no longer hides the path, so a warning is
        logged. The line holds only the numbers: never an email, an IP or a password.
        """
        elapsed = self.clock() - started
        remaining = self.floor_seconds - elapsed
        if remaining > 0:
            await self.sleep(remaining)
        else:
            logger.warning(
                "login.timing_floor_exceeded floor_ms=%d work_ms=%d",
                round(self.floor_seconds * 1000),
                round(elapsed * 1000),
            )

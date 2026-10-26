"""
The Valkey parts of POST /ask against a real Valkey (ADR 0033, decision 3): the rate limit, the in-flight set that
heals itself, the metering flag, and fail-closed behaviour when Valkey is down or slow. Each test uses its own ids and
a fake clock, so nothing sleeps except the two tests that prove a real timeout.
"""

import asyncio
import math
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from grounded_answers.ask.valkey import (
    CALL_TIMEOUT_SECONDS,
    AskLimiter,
    InFlightSet,
    MeteringFlagStore,
)
from grounded_answers.rate_limit import LimiterUnavailable
from tests.db.auth_support import VALKEY_TEST_URL, flush_test_valkey

pytestmark = [pytest.mark.db, pytest.mark.anyio]

KEY = secrets.token_bytes(48)
START_MS = 1_800_000_000_000


class FakeClock:
    def __init__(self, start_ms: int = START_MS) -> None:
        self.now = start_ms

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


@pytest.fixture
async def cache() -> AsyncIterator[Redis]:
    flush_test_valkey()
    client = Redis.from_url(VALKEY_TEST_URL, socket_timeout=0.5, socket_connect_timeout=0.5)
    try:
        yield client
    finally:
        await client.aclose()


async def _keys(cache: Redis) -> list[str]:
    return sorted(
        str(key.decode() if isinstance(key, bytes) else key)
        for key in await cache.keys("grounded:ask:*")
    )


def _raw_forms(*ids: uuid.UUID) -> list[str]:
    forms: list[str] = []
    for value in ids:
        forms += [str(value), value.hex, str(value).upper()]
    return forms


# ---- rate limit ----


async def test_the_user_limit_refuses_the_sixth_question_in_a_minute(cache: Redis) -> None:
    clock = FakeClock()
    limiter = AskLimiter(cache, KEY, clock, tenant_limit=20, user_limit=5)
    tenant, user = uuid.uuid4(), uuid.uuid4()
    for _ in range(5):
        assert await limiter.admit(tenant, user) is None
    wait = await limiter.admit(tenant, user)
    assert wait is not None and 1 <= wait <= 60
    # the window ends at the next whole minute: the wait is the time left in this one, rounded up
    assert wait == max(1, math.ceil((60_000 - START_MS % 60_000) / 1000))


async def test_the_tenant_limit_is_shared_by_its_users_and_other_tenants_are_unaffected(
    cache: Redis,
) -> None:
    limiter = AskLimiter(cache, KEY, FakeClock(), tenant_limit=3, user_limit=5)
    tenant, other = uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        assert await limiter.admit(tenant, uuid.uuid4()) is None  # a different user each time
    assert await limiter.admit(tenant, uuid.uuid4()) is not None
    assert await limiter.admit(other, uuid.uuid4()) is None


async def test_the_next_minute_starts_fresh(cache: Redis) -> None:
    clock = FakeClock()
    limiter = AskLimiter(cache, KEY, clock, tenant_limit=20, user_limit=1)
    tenant, user = uuid.uuid4(), uuid.uuid4()
    assert await limiter.admit(tenant, user) is None
    assert await limiter.admit(tenant, user) is not None
    clock.advance(60_000)
    assert await limiter.admit(tenant, user) is None


async def test_a_refused_attempt_still_counts(cache: Redis) -> None:
    clock = FakeClock()
    limiter = AskLimiter(cache, KEY, clock, tenant_limit=20, user_limit=1)
    tenant, user = uuid.uuid4(), uuid.uuid4()
    await limiter.admit(tenant, user)
    for _ in range(3):
        assert await limiter.admit(tenant, user) is not None
    counts: list[int] = []
    for key in await cache.keys("grounded:ask:rate:user:*"):
        raw = await cache.get(key)
        assert raw is not None
        counts.append(int(raw))
    assert counts == [4]


# ---- in-flight set ----


async def test_the_in_flight_limit_and_release(cache: Redis) -> None:
    clock = FakeClock()
    slots = InFlightSet(cache, KEY, clock, limit=2, hold_seconds=17)
    tenant = uuid.uuid4()
    assert await slots.acquire(tenant, "r1") is True
    assert await slots.acquire(tenant, "r2") is True
    assert await slots.acquire(tenant, "r3") is False
    await slots.release(tenant, "r1")
    assert await slots.acquire(tenant, "r3") is True
    await slots.release(tenant, "never-acquired")  # harmless


async def test_in_flight_slots_are_per_tenant(cache: Redis) -> None:
    slots = InFlightSet(cache, KEY, FakeClock(), limit=1, hold_seconds=17)
    a, b = uuid.uuid4(), uuid.uuid4()
    assert await slots.acquire(a, "r1") is True
    assert await slots.acquire(b, "r2") is True
    assert await slots.acquire(a, "r3") is False


async def test_a_leaked_slot_expires_on_its_own_and_other_traffic_cannot_keep_it_alive(
    cache: Redis,
) -> None:
    clock = FakeClock()
    slots = InFlightSet(cache, KEY, clock, limit=2, hold_seconds=17)
    tenant = uuid.uuid4()
    assert await slots.acquire(tenant, "killed-1") is True  # these two requests never release
    assert await slots.acquire(tenant, "killed-2") is True
    assert await slots.acquire(tenant, "blocked") is False
    # healthy traffic keeps coming for a while: each request takes a slot only after the leak has expired
    for _ in range(3):
        clock.advance(5_000)
        await slots.acquire(tenant, f"busy-{clock.now}")  # refused while both leaks are alive
    clock.advance(10_000)  # now past the 17 s hold of the first two
    assert await slots.acquire(tenant, "fresh-1") is True
    assert await slots.acquire(tenant, "fresh-2") is True
    await slots.release(tenant, "fresh-1")
    await slots.release(tenant, "fresh-2")
    assert await _keys(cache) == []  # an empty set is deleted: nothing of the leak is left


async def test_the_in_flight_key_expires_when_unused(cache: Redis) -> None:
    slots = InFlightSet(cache, KEY, FakeClock(), limit=2, hold_seconds=17)
    await slots.acquire(uuid.uuid4(), "r1")
    (key,) = await _keys(cache)
    assert 0 < await cache.pttl(key) <= 18_000 + 1000


# ---- metering flag ----


async def test_the_flag_is_per_tenant_and_short_lived(cache: Redis) -> None:
    flag = MeteringFlagStore(cache, KEY)
    a, b = uuid.uuid4(), uuid.uuid4()
    assert await flag.is_set(a) is False
    await flag.set(a)
    assert await flag.is_set(a) is True
    assert await flag.is_set(b) is False
    (key,) = await _keys(cache)
    assert 280 <= await cache.ttl(key) <= 300


async def test_the_flag_expires(cache: Redis) -> None:
    flag = MeteringFlagStore(cache, KEY, ttl_seconds=1)
    tenant = uuid.uuid4()
    await flag.set(tenant)
    assert await flag.is_set(tenant) is True
    await asyncio.sleep(1.2)
    assert await flag.is_set(tenant) is False


# ---- no raw ids in Valkey ----


async def test_no_key_holds_a_raw_id(cache: Redis) -> None:
    tenant, user = uuid.uuid4(), uuid.uuid4()
    await AskLimiter(cache, KEY, FakeClock(), tenant_limit=20, user_limit=5).admit(tenant, user)
    await InFlightSet(cache, KEY, FakeClock(), limit=2, hold_seconds=17).acquire(tenant, "r1")
    await MeteringFlagStore(cache, KEY).set(tenant)
    keys = await _keys(cache)
    assert len(keys) == 4  # tenant count, user count, in-flight set, flag
    for key in keys:
        for form in _raw_forms(tenant, user):
            assert form not in key


# ---- fail closed ----


async def test_every_operation_fails_closed_when_valkey_is_unreachable() -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_timeout=0.5, socket_connect_timeout=0.5)
    try:
        tenant, user = uuid.uuid4(), uuid.uuid4()
        with pytest.raises(LimiterUnavailable):
            await AskLimiter(dead, KEY, FakeClock(), tenant_limit=20, user_limit=5).admit(
                tenant, user
            )
        slots = InFlightSet(dead, KEY, FakeClock(), limit=2, hold_seconds=17)
        with pytest.raises(LimiterUnavailable):
            await slots.acquire(tenant, "r1")
        with pytest.raises(LimiterUnavailable):
            await slots.release(tenant, "r1")
        flag = MeteringFlagStore(dead, KEY)
        with pytest.raises(LimiterUnavailable):
            await flag.is_set(tenant)
        with pytest.raises(LimiterUnavailable):
            await flag.set(tenant)
    finally:
        await dead.aclose()


async def test_a_valkey_that_accepts_but_never_answers_is_cut_off_by_the_call_timeout() -> None:
    """The client's own socket timeout is long here, so only the per-call timeout can end the wait."""

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(30)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    hung = Redis.from_url(
        f"redis://127.0.0.1:{port}/0", socket_timeout=30, socket_connect_timeout=5
    )
    try:
        started = time.monotonic()
        with pytest.raises(LimiterUnavailable):
            await MeteringFlagStore(hung, KEY).is_set(uuid.uuid4())
        assert time.monotonic() - started < CALL_TIMEOUT_SECONDS + 1.0
    finally:
        await hung.aclose()
        server.close()


# ---- the shared client in the real app ----


def test_the_apps_shared_valkey_client_has_short_socket_timeouts(client: TestClient) -> None:
    """Login and upload limits share this client, so a slow Valkey fails them closed too (ADR 0033, D9)."""
    kwargs: dict[str, Any] = client.app.state.cache.connection_pool.connection_kwargs  # type: ignore[attr-defined]
    assert kwargs["socket_timeout"] == 0.5
    assert kwargs["socket_connect_timeout"] == 0.5

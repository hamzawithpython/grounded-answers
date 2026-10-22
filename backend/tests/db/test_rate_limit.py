"""
Login and refresh limits against a real Valkey (ADR 0014). Each test uses its own random email and IP, and
a fake clock, so nothing sleeps and no test depends on another.
"""

import asyncio
import secrets
from collections.abc import Iterator
from typing import Any

import pytest
from redis.asyncio import Redis

from grounded_answers.rate_limit import (
    IP_LIMIT,
    PAIR_LIMIT,
    REFRESH_LIMIT,
    LimiterUnavailable,
    LoginLimiter,
    account_delay_ms,
    unlock_account,
)
from tests.db.auth_support import VALKEY_TEST_URL, flush_test_valkey, new_test_ip

pytestmark = pytest.mark.db

RATE_KEY = secrets.token_bytes(48)


@pytest.fixture(autouse=True)
def fresh_valkey() -> Iterator[None]:
    """Empty the limiter's Valkey database before each test. Counters live 10 real minutes, and
    the fake clock sits in one fixed window, so without this a test sees earlier tests' failures
    and earlier runs' failures."""
    flush_test_valkey()
    yield


class FakeClock:
    def __init__(self, start_ms: int = 1_800_000_000_000) -> None:
        self.now = start_ms

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


@pytest.fixture
async def cache() -> Any:
    client: Redis = Redis.from_url(VALKEY_TEST_URL, socket_connect_timeout=2, socket_timeout=2)
    yield client
    await client.aclose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(cache: Redis, clock: FakeClock) -> LoginLimiter:
    return LoginLimiter(cache, RATE_KEY, clock)


def _email() -> str:
    return f"user-{secrets.token_hex(6)}@limits.test"


async def _fail(limiter: LoginLimiter, email: str, ip: str, times: int = 1) -> None:
    for _ in range(times):
        await limiter.record_failure(email, ip)


async def _read_int(cache: Redis, key: str) -> int:
    raw = await cache.get(key)
    assert raw is not None, f"the key {key!r} must exist"
    return int(raw)


@pytest.mark.anyio
async def test_concurrent_failures_are_counted_exactly(limiter: LoginLimiter, cache: Redis) -> None:
    """Forty simultaneous failures for one pair: the counter is exactly 40, and the lock is set."""
    email, ip = _email(), new_test_ip()
    await asyncio.gather(*(limiter.record_failure(email, ip) for _ in range(40)))
    keys = limiter._keys(email, ip, limiter.clock())
    assert await _read_int(cache, keys["pair_count"]) == 40
    assert await _read_int(cache, keys["pair_lock"]) > limiter.clock()


@pytest.mark.anyio
async def test_fifth_failure_locks_the_pair_and_the_sixth_attempt_is_refused(
    limiter: LoginLimiter,
) -> None:
    email, ip = _email(), new_test_ip()
    await _fail(limiter, email, ip, PAIR_LIMIT - 1)
    assert (await limiter.check(email, ip)).refused is False, "four failures do not lock"
    await _fail(limiter, email, ip)
    verdict = await limiter.check(email, ip)
    assert verdict.refused is True
    assert verdict.retry_after_seconds == 900, "15 minutes"


@pytest.mark.anyio
async def test_lock_is_per_email_and_ip_pair(limiter: LoginLimiter, clock: FakeClock) -> None:
    email = _email()
    locked_ip, other_ip = new_test_ip(), new_test_ip()
    await _fail(limiter, email, locked_ip, PAIR_LIMIT)
    assert (await limiter.check(email, locked_ip)).refused is True
    clock.advance(1000)  # the account delay (1 s) applies to every IP, so wait it out first
    assert (await limiter.check(email, other_ip)).refused is False, "another IP is not locked out"


@pytest.mark.anyio
async def test_refused_attempts_do_not_extend_the_lock(
    limiter: LoginLimiter, clock: FakeClock
) -> None:
    email, ip = _email(), new_test_ip()
    await _fail(limiter, email, ip, PAIR_LIMIT)
    for _ in range(20):
        assert (await limiter.check(email, ip)).refused is True
    clock.advance(15 * 60 * 1000)
    assert (await limiter.check(email, ip)).refused is False, "the lock ends on time, not later"


@pytest.mark.anyio
async def test_progressive_delay_grows_one_two_four_seconds(
    limiter: LoginLimiter, clock: FakeClock
) -> None:
    """Five failures from five IPs (so no pair locks), then the delays are checked one by one."""
    email = _email()
    for _ in range(5):
        await _fail(limiter, email, new_test_ip())
    first = await limiter.check(email, new_test_ip())
    assert first.refused and first.retry_after_seconds == 1
    clock.advance(1000)
    assert (await limiter.check(email, new_test_ip())).refused is False

    await _fail(limiter, email, new_test_ip())  # the sixth failure: delay 2 s
    assert (await limiter.check(email, new_test_ip())).retry_after_seconds == 2
    clock.advance(2000)
    await _fail(limiter, email, new_test_ip())  # the seventh: delay 4 s
    assert (await limiter.check(email, new_test_ip())).retry_after_seconds == 4
    assert account_delay_ms(7) == 4000


@pytest.mark.anyio
async def test_per_ip_limit_blocks_the_ip_for_the_window(
    limiter: LoginLimiter, clock: FakeClock
) -> None:
    ip = new_test_ip()
    for _ in range(IP_LIMIT):
        await _fail(limiter, _email(), ip)
    verdict = await limiter.check(_email(), ip)
    assert verdict.refused is True
    assert verdict.retry_after_seconds > 0
    clock.advance(10 * 60 * 1000)
    assert (await limiter.check(_email(), ip)).refused is False


@pytest.mark.anyio
async def test_hard_lock_after_fifty_failures_and_operator_release(
    limiter: LoginLimiter, cache: Redis, clock: FakeClock
) -> None:
    """Fifty failures across ten IPs (five each, so no pair locks), then 24 hours, then the operator unlock."""
    email = _email()
    for _ in range(10):
        ip = new_test_ip()
        await _fail(limiter, email, ip, PAIR_LIMIT)
    verdict = await limiter.check(email, new_test_ip())
    assert verdict.refused is True
    assert verdict.retry_after_seconds == 24 * 60 * 60

    removed = await unlock_account(cache, RATE_KEY, email, clock())
    assert removed >= 1
    assert (await limiter.check(email, new_test_ip())).refused is False


@pytest.mark.anyio
async def test_hard_lock_expires_after_twenty_four_hours_without_release(
    limiter: LoginLimiter, clock: FakeClock
) -> None:
    email = _email()
    for _ in range(10):
        await _fail(limiter, email, new_test_ip(), PAIR_LIMIT)
    assert (await limiter.check(email, new_test_ip())).refused is True
    clock.advance(24 * 60 * 60 * 1000)
    assert (await limiter.check(email, new_test_ip())).refused is False


@pytest.mark.anyio
async def test_refresh_limit_counts_per_ip(limiter: LoginLimiter) -> None:
    ip = new_test_ip()
    verdicts = [await limiter.check_refresh(ip) for _ in range(REFRESH_LIMIT + 1)]
    assert all(v.refused is False for v in verdicts[:REFRESH_LIMIT])
    assert verdicts[-1].refused is True
    assert (await limiter.check_refresh(new_test_ip())).refused is False, (
        "another IP has its own budget"
    )


@pytest.mark.anyio
async def test_success_clears_the_pair_and_account_counters(
    limiter: LoginLimiter, cache: Redis
) -> None:
    email, ip = _email(), new_test_ip()
    await _fail(limiter, email, ip, PAIR_LIMIT - 1)
    await limiter.reset_after_success(email, ip)
    await _fail(limiter, email, ip, PAIR_LIMIT - 1)
    assert (await limiter.check(email, ip)).refused is False, (
        "the count started again after the success"
    )


@pytest.mark.anyio
async def test_no_raw_email_or_ip_appears_in_any_key_or_value(
    limiter: LoginLimiter, cache: Redis
) -> None:
    email = f"private-person-{secrets.token_hex(4)}@private-domain.test"
    ip = "203.0.113.77"
    await _fail(limiter, email, ip, PAIR_LIMIT)
    await limiter.check_refresh(ip)
    seen: list[str] = []
    async for key in cache.scan_iter(match="auth:rl:*"):
        name = key.decode() if isinstance(key, bytes) else key
        seen.append(name)
        seen.append(str(await cache.get(name)))
    assert seen, "the limiter must have written keys"
    blob = "\n".join(seen)
    for raw in (email, "private-person", "private-domain", ip, "203.0.113"):
        assert raw not in blob


@pytest.mark.anyio
async def test_every_key_has_an_expiry(limiter: LoginLimiter, cache: Redis) -> None:
    email, ip = _email(), new_test_ip()
    await _fail(limiter, email, ip, PAIR_LIMIT)
    await limiter.check_refresh(ip)
    async for key in cache.scan_iter(match="auth:rl:*"):
        ttl = await cache.pttl(key)
        # -1 means the key has no expiry. -2 means it already expired, which is the expiry working.
        assert ttl != -1, f"a key without expiry: {key!r}"


@pytest.mark.anyio
async def test_valkey_down_raises_limiter_unavailable(clock: FakeClock) -> None:
    dead: Redis = Redis.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.2, socket_timeout=0.2
    )
    try:
        broken = LoginLimiter(dead, RATE_KEY, clock)
        with pytest.raises(LimiterUnavailable):
            await broken.check(_email(), new_test_ip())
        with pytest.raises(LimiterUnavailable):
            await broken.record_failure(_email(), new_test_ip())
        with pytest.raises(LimiterUnavailable):
            await broken.check_refresh(new_test_ip())
    finally:
        await dead.aclose()

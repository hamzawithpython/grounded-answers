"""
Login and refresh limits, kept in Valkey (ADR 0004, ADR 0014).

Limits, all keyed by HMAC-SHA256 of the value (no raw email or IP is ever stored, ADR 0014):
- Email + IP pair: 5 failures in 15 minutes lock that pair for 15 minutes.
- IP: 20 failed logins in 10 minutes block the IP for the rest of the window.
- Account (email): after 5 failures in an hour, each further attempt waits 1, 2, 4 ... seconds,
  capped at 30 seconds.
- Account, hard: 50 failures in an hour lock the account for 24 hours. Only the operator's
  unlock-account command clears it early.
- Refresh, per IP: 300 refresh requests in 10 minutes.

Atomicity: each failure is recorded by one Lua script, so concurrent failures are counted exactly
and the lock starts at exactly the fifth failure. Keys carry an expiry, set in the same script.

Time: the caller supplies the clock (milliseconds). Fixed windows are computed from it, and locks
store an absolute "until" time. A test can move time forward without sleeping. Multiple app
instances must have roughly synchronised clocks (deployment note in ADR 0014).

Valkey down: login fails closed (503), refresh fails open. Both log a warning (decision 7B).
"""

import hashlib
import hmac
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

Clock = Callable[[], int]

PAIR_LIMIT: Final = 5
PAIR_WINDOW_MS: Final = 15 * 60 * 1000
PAIR_LOCK_MS: Final = 15 * 60 * 1000
IP_LIMIT: Final = 20
IP_WINDOW_MS: Final = 10 * 60 * 1000
ACCOUNT_DELAY_START: Final = 5
ACCOUNT_DELAY_CAP_MS: Final = 30 * 1000
ACCOUNT_WINDOW_MS: Final = 60 * 60 * 1000
HARD_LIMIT: Final = 50
HARD_LOCK_MS: Final = 24 * 60 * 60 * 1000
REFRESH_LIMIT: Final = 300
REFRESH_WINDOW_MS: Final = 10 * 60 * 1000
SLACK_MS: Final = 1000
FAIL_CLOSED_RETRY_SECONDS: Final = 30

PREFIX: Final = "auth:rl"

# Atomic failure record. Counts and sets locks in one step, with expiry on every key.
RECORD_FAILURE_LUA: Final = """
local now = tonumber(ARGV[1])
local pc = redis.call('INCR', KEYS[1])
if pc == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[3]) end
if pc >= tonumber(ARGV[2]) then
  redis.call('SET', KEYS[2], now + tonumber(ARGV[4]), 'PX', ARGV[4])
end
local ic = redis.call('INCR', KEYS[3])
if ic == 1 then redis.call('PEXPIRE', KEYS[3], ARGV[5]) end
local ac = redis.call('INCR', KEYS[4])
if ac == 1 then redis.call('PEXPIRE', KEYS[4], ARGV[6]) end
local start = tonumber(ARGV[7])
if ac >= start then
  local delay = math.floor(math.min(1000 * (2 ^ (ac - start)), tonumber(ARGV[8])))
  redis.call('SET', KEYS[5], now + delay, 'PX', delay + 1000)
end
if ac >= tonumber(ARGV[9]) then
  redis.call('SET', KEYS[6], now + tonumber(ARGV[10]), 'PX', ARGV[10])
end
return {pc, ic, ac}
"""

# Count one request in a fixed window. Returns the count after this request.
INCR_WITH_EXPIRY_LUA: Final = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[1]) end
return c
"""


class LimiterUnavailable(Exception):
    """Valkey did not answer in time, or refused the connection."""


@dataclass(frozen=True)
class Verdict:
    refused: bool
    retry_after_seconds: int = 0


ALLOWED: Final = Verdict(refused=False)


def account_delay_ms(failures: int) -> int:
    """The wait after the given number of account failures (1 s, 2 s, 4 s ... capped at 30 s)."""
    if failures < ACCOUNT_DELAY_START:
        return 0
    delay: int = min(1000 * (2 ** (failures - ACCOUNT_DELAY_START)), ACCOUNT_DELAY_CAP_MS)
    return delay


def window_index(now_ms: int, window_ms: int) -> int:
    return now_ms // window_ms


def window_end_ms(now_ms: int, window_ms: int) -> int:
    return (window_index(now_ms, window_ms) + 1) * window_ms


def retry_seconds(remaining_ms: int) -> int:
    """Whole seconds to wait, rounded up, never less than one second for a refusal."""
    return max(1, math.ceil(remaining_ms / 1000))


def digest(hmac_key: bytes, namespace: str, value: str) -> str:
    """Keyed hash of a value. Raw emails and IPs never reach Valkey (ADR 0014)."""
    return hmac.new(hmac_key, f"{namespace}:{value}".encode(), hashlib.sha256).hexdigest()


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


class LoginLimiter:
    """Login and refresh limits. One instance per app; the clock can be replaced in tests."""

    def __init__(self, cache: Redis, hmac_key: bytes, clock: Clock) -> None:
        self.cache = cache
        self.hmac_key = hmac_key
        self.clock = clock
        self._record = cache.register_script(RECORD_FAILURE_LUA)
        self._incr = cache.register_script(INCR_WITH_EXPIRY_LUA)

    def _keys(self, email: str, client_ip: str, now: int) -> dict[str, str]:
        pair = digest(self.hmac_key, "pair", f"{email}|{client_ip}")
        ip = digest(self.hmac_key, "ip", client_ip)
        account = digest(self.hmac_key, "account", email)
        return {
            "pair_count": f"{PREFIX}:paircount:{pair}:{window_index(now, PAIR_WINDOW_MS)}",
            "pair_lock": f"{PREFIX}:pairlock:{pair}",
            "ip_count": f"{PREFIX}:ipfail:{ip}:{window_index(now, IP_WINDOW_MS)}",
            "account_count": f"{PREFIX}:acctfail:{account}:{window_index(now, ACCOUNT_WINDOW_MS)}",
            "account_delay": f"{PREFIX}:acctdelay:{account}",
            "hard_lock": f"{PREFIX}:hardlock:{account}",
        }

    async def check(self, email: str, client_ip: str) -> Verdict:
        """
        Read-only. Refused attempts are not counted (ADR 0014), so a refusal never extends a lock.
        Raise LimiterUnavailable if Valkey does not answer.
        """
        now = self.clock()
        keys = self._keys(email, client_ip, now)
        try:
            values = await self.cache.mget(
                keys["pair_lock"], keys["ip_count"], keys["account_delay"], keys["hard_lock"]
            )
        except RedisError as exc:
            raise LimiterUnavailable from exc
        pair_lock, ip_count, account_delay, hard_lock = (_as_int(v) for v in values)
        waits: list[int] = []
        if pair_lock > now:
            waits.append(pair_lock - now)
        if account_delay > now:
            waits.append(account_delay - now)
        if hard_lock > now:
            waits.append(hard_lock - now)
        if ip_count >= IP_LIMIT:
            waits.append(window_end_ms(now, IP_WINDOW_MS) - now)
        if not waits:
            return ALLOWED
        return Verdict(refused=True, retry_after_seconds=retry_seconds(max(waits)))

    async def record_failure(self, email: str, client_ip: str) -> None:
        """Count one failed login. One Lua script, so the counts and the locks are consistent."""
        now = self.clock()
        k = self._keys(email, client_ip, now)
        try:
            await self._record(
                keys=[
                    k["pair_count"],
                    k["pair_lock"],
                    k["ip_count"],
                    k["account_count"],
                    k["account_delay"],
                    k["hard_lock"],
                ],
                args=[
                    now,
                    PAIR_LIMIT,
                    PAIR_WINDOW_MS,
                    PAIR_LOCK_MS,
                    IP_WINDOW_MS,
                    ACCOUNT_WINDOW_MS,
                    ACCOUNT_DELAY_START,
                    ACCOUNT_DELAY_CAP_MS,
                    HARD_LIMIT,
                    HARD_LOCK_MS,
                ],
            )
        except RedisError as exc:
            raise LimiterUnavailable from exc

    async def reset_after_success(self, email: str, client_ip: str) -> None:
        """A successful login clears the pair and account counters for the current windows."""
        now = self.clock()
        k = self._keys(email, client_ip, now)
        try:
            await self.cache.delete(k["pair_count"], k["account_count"], k["account_delay"])
        except RedisError as exc:
            raise LimiterUnavailable from exc

    async def check_refresh(self, client_ip: str) -> Verdict:
        """Count one refresh request per IP. Refused when the count passes the limit."""
        now = self.clock()
        key = f"{PREFIX}:refresh:{digest(self.hmac_key, 'ip', client_ip)}:{window_index(now, REFRESH_WINDOW_MS)}"
        try:
            count = int(await self._incr(keys=[key], args=[REFRESH_WINDOW_MS + SLACK_MS]))
        except RedisError as exc:
            raise LimiterUnavailable from exc
        if count > REFRESH_LIMIT:
            return Verdict(
                refused=True,
                retry_after_seconds=retry_seconds(window_end_ms(now, REFRESH_WINDOW_MS) - now),
            )
        return ALLOWED


async def unlock_account(cache: Redis, hmac_key: bytes, email: str, now: int) -> int:
    """
    Operator unlock (ADR 0014): remove the hard lock and the current-hour counters for the account.

    Returns the number of keys removed. The counters of earlier hours expire on their own.
    """
    account = digest(hmac_key, "account", email)
    keys = [
        f"{PREFIX}:hardlock:{account}",
        f"{PREFIX}:acctdelay:{account}",
        f"{PREFIX}:acctfail:{account}:{window_index(now, ACCOUNT_WINDOW_MS)}",
    ]
    removed: int = await cache.delete(*keys)
    return removed


def wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000

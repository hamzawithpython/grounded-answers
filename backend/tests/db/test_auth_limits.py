"""
Login and refresh limits over HTTP, with the real app and the real database (ADR 0014).

The app's limiter uses a frozen or advanced fake clock, so Retry-After values are exact and nothing sleeps.
Client IPs are varied by replacing the route's _client_ip helper, because TestClient always reports the
same address.
"""

import logging
import re
import secrets
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from redis.asyncio import Redis

from grounded_answers import auth_api
from grounded_answers.admin_cli import main as admin_main
from grounded_answers.rate_limit import LoginLimiter
from grounded_answers.timing import TimingFloor
from tests.db.auth_support import VALKEY_TEST_URL, Accounts
from tests.db.database import DbHandle
from tests.db.test_admin_cli import _url

pytestmark = pytest.mark.db

LIMITED = {"detail": "Too many attempts. Try again later."}
UNAVAILABLE = {"detail": "Sign-in is temporarily unavailable. Try again shortly."}
WRONG = "not-the-password-at-all"


HOUR_MS = 60 * 60 * 1000


class FrozenClock:
    def __init__(self, start_ms: int = 500_000 * HOUR_MS) -> None:
        self.now = start_ms

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


@pytest.fixture
def clock(client: TestClient) -> Iterator[FrozenClock]:
    fake = FrozenClock()
    client.app.state.limiter.clock = fake  # type: ignore[attr-defined]
    yield fake


@pytest.fixture
def ips(monkeypatch: MonkeyPatch) -> Iterator[list[str]]:
    """Each login request takes the next address from this list (the default is 'testclient')."""
    queue: list[str] = []
    monkeypatch.setattr(
        auth_api, "_client_ip", lambda request: queue.pop(0) if queue else "192.0.2.1"
    )
    yield queue


def _new_ip() -> str:
    return f"198.51.100.{secrets.randbelow(250) + 1}"


def _login(
    client: TestClient, ips: list[str], email: str, password: str, ip: str | None = None
) -> Any:
    ips.insert(0, ip or _new_ip())
    return client.post("/auth/login", json={"email": email, "password": password})


def _fingerprint(response: Any) -> tuple[int, bytes, str | None]:
    return response.status_code, response.content, response.headers.get("retry-after")


def test_fifty_failures_give_identical_responses_for_known_and_unknown_emails(
    client: TestClient, accounts: Accounts, clock: FrozenClock, ips: list[str]
) -> None:
    """
    After 50 failures, a known email and an unknown email get byte-identical responses: status, body and
    Retry-After (ADR 0014, decision 6). The two emails are attacked in alternation, so both reach their
    fiftieth failure at the same clock time. Their 24-hour locks then end at the same time too.
    """
    unknown = f"nobody-{secrets.token_hex(4)}@indus.test"
    for _ in range(10):
        ip = _new_ip()
        for _ in range(5):
            clock.advance(
                31_000
            )  # past the 30 s cap, so the account delay never refuses a counted attempt
            for email in (accounts.admin_email, unknown):  # both emails at the same clock time
                _login(client, ips, email, WRONG, ip=ip)
    probe_ip = _new_ip()
    known = _login(client, ips, accounts.admin_email, WRONG, ip=probe_ip)
    missing = _login(client, ips, unknown, WRONG, ip=probe_ip)
    assert known.status_code == 429
    assert known.json() == LIMITED
    assert 1 <= int(known.headers["retry-after"]) <= 86400
    assert _fingerprint(known) == _fingerprint(missing)


def test_a_locked_pair_is_refused_and_another_ip_is_not(
    client: TestClient, accounts: Accounts, clock: FrozenClock, ips: list[str]
) -> None:
    ip = _new_ip()
    for _ in range(5):
        _login(client, ips, accounts.admin_email, WRONG, ip=ip)
    locked = _login(client, ips, accounts.admin_email, WRONG, ip=ip)
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "900"
    clock.advance(1000)  # the account delay (1 s) applies to every IP; wait it out
    other = _login(client, ips, accounts.admin_email, WRONG, ip=_new_ip())
    assert other.status_code == 401


def test_progressive_delay_is_enforced_over_http(
    client: TestClient, accounts: Accounts, clock: FrozenClock, ips: list[str]
) -> None:
    for _ in range(5):
        _login(client, ips, accounts.admin_email, WRONG)
    waiting = _login(client, ips, accounts.admin_email, WRONG)
    assert waiting.status_code == 429
    assert waiting.headers["retry-after"] == "1"
    clock.advance(1000)
    allowed = _login(client, ips, accounts.admin_email, WRONG)
    assert allowed.status_code == 401
    assert allowed.json() == {"detail": "Invalid email or password"}


def test_success_resets_the_counters(
    client: TestClient, accounts: Accounts, clock: FrozenClock, ips: list[str]
) -> None:
    ip = _new_ip()
    for _ in range(4):
        _login(client, ips, accounts.admin_email, WRONG, ip=ip)
    ok = _login(client, ips, accounts.admin_email, accounts.password, ip=ip)
    assert ok.status_code == 200
    for _ in range(4):
        _login(client, ips, accounts.admin_email, WRONG, ip=ip)
    assert _login(client, ips, accounts.admin_email, accounts.password, ip=ip).status_code == 200, (
        "four failures after a success do not lock the pair"
    )


def test_valkey_down_fails_login_closed_with_a_logged_warning(
    client: TestClient, accounts: Accounts, ips: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2, socket_timeout=0.2)
    real = client.app.state.limiter  # type: ignore[attr-defined]
    client.app.state.limiter = LoginLimiter(dead, accounts.rate_key.encode("ascii"), real.clock)  # type: ignore[attr-defined]
    try:
        with caplog.at_level(logging.WARNING, logger="grounded_answers.auth"):
            response = _login(client, ips, accounts.admin_email, accounts.password)
    finally:
        client.app.state.limiter = real  # type: ignore[attr-defined]
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert response.headers["retry-after"] == "30"
    assert "login.limiter_unavailable action=fail_closed" in caplog.text
    assert accounts.password not in caplog.text


def test_valkey_down_leaves_refresh_working_with_a_logged_warning(
    client: TestClient, accounts: Accounts, ips: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    login = _login(client, ips, accounts.admin_email, accounts.password)
    assert login.status_code == 200
    match = re.search(r"__Secure-refresh=([^;]+)", login.headers["set-cookie"])
    assert match is not None
    raw = match.group(1)

    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2, socket_timeout=0.2)
    real = client.app.state.limiter  # type: ignore[attr-defined]
    client.app.state.limiter = LoginLimiter(dead, accounts.rate_key.encode("ascii"), real.clock)  # type: ignore[attr-defined]
    try:
        with caplog.at_level(logging.WARNING, logger="grounded_answers.auth"):
            response = client.post(
                "/auth/refresh",
                headers={"Origin": "http://127.0.0.1:3000", "Cookie": f"__Secure-refresh={raw}"},
            )
    finally:
        client.app.state.limiter = real  # type: ignore[attr-defined]
    assert response.status_code == 200, "refresh fails open: rotation still protects the token"
    assert "refresh.limiter_unavailable action=fail_open" in caplog.text


def test_every_login_path_waits_for_the_timing_floor(
    client: TestClient, accounts: Accounts, ips: list[str], monkeypatch: MonkeyPatch
) -> None:
    """Unknown email, wrong password and success all end at the floor (ADR 0014, R11). Fake time."""
    now = [0.0]
    sleeps: list[float] = []

    def fake_clock() -> float:
        return now[0]

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    floor = TimingFloor(floor_seconds=0.5, clock=fake_clock, sleep=fake_sleep)
    monkeypatch.setattr(client.app.state, "timing", floor)  # type: ignore[attr-defined]
    for email, password in (
        (f"nobody-{secrets.token_hex(4)}@indus.test", WRONG),
        (accounts.admin_email, WRONG),
        (accounts.admin_email, accounts.password),
    ):
        before = now[0]
        _login(client, ips, email, password)
        assert now[0] - before == pytest.approx(0.5), f"{email} path must end at the floor"
    assert len(sleeps) == 3


def test_operator_unlock_clears_the_hard_lock_and_audits_it(
    grounded_db: DbHandle,
    client: TestClient,
    accounts: Accounts,
    clock: FrozenClock,
    ips: list[str],
    monkeypatch: MonkeyPatch,
) -> None:
    email = accounts.admin_email
    for _ in range(10):
        ip = _new_ip()
        for _ in range(5):
            _login(client, ips, email, WRONG, ip=ip)
    assert _login(client, ips, email, accounts.password).status_code == 429

    monkeypatch.setenv("MIGRATION_DATABASE_URL", _url(grounded_db, "migration_role"))
    monkeypatch.setenv("REDIS_URL", VALKEY_TEST_URL)
    monkeypatch.setenv("AUTH_RATE_LIMIT_HMAC_KEY", accounts.rate_key)
    from click.testing import CliRunner

    with psycopg.connect(grounded_db.admin_test_url()) as conn:
        slug_row = conn.execute(
            "SELECT slug FROM grounded.tenants WHERE id = %s", (accounts.seeded.a.tenant,)
        ).fetchone()
    assert slug_row is not None
    slug = str(slug_row[0])

    result = CliRunner().invoke(admin_main, ["unlock-account", slug, email])
    assert result.exit_code == 0, result.output
    assert "Keys removed:" in result.output
    assert accounts.rate_key not in result.output

    assert _login(client, ips, email, accounts.password).status_code == 200, (
        "the account is usable again"
    )
    with psycopg.connect(grounded_db.admin_test_url()) as conn:
        audit = conn.execute(
            "SELECT detail::text FROM grounded.audit_log "
            "WHERE tenant_id = %s AND action = 'account.unlock' ORDER BY created_at DESC LIMIT 1",
            (accounts.seeded.a.tenant,),
        ).fetchone()
    assert audit is not None, "the unlock must be audited"
    assert "keys_removed" in audit[0]
    assert email not in audit[0], "the audit detail never holds the email"

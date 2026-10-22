"""
Shared fixtures and helpers for the auth HTTP tests (ADR 0004).

Registered for every DB test in tests/db/conftest.py, so test modules can request `accounts` and
`client` without importing them. Helpers that tests call directly are imported explicitly.
"""

import ipaddress
import itertools
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
import redis
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from grounded_answers.auth_keys import KeyRing, load_key_ring, write_new_key_file
from grounded_answers.config import get_settings
from grounded_answers.main import create_app
from grounded_answers.passwords import hash_password
from grounded_answers.runtime import selector_loop_factory
from tests.db.database import DbHandle
from tests.db.seed import Seeded
from tests.db.test_orm_models import app_url

SELECTOR: dict[str, Any] = {"loop_factory": selector_loop_factory}
LOGIN_BODY_MARKER = "MARKER-DO-NOT-ECHO"
# PyJWT's typing does not allow None, but alg=none tokens are signed with no key.
UNSIGNED_KEY: Any = None


VALKEY_TEST_URL = os.environ.get("TEST_VALKEY_URL", "redis://127.0.0.1:6379/15")

_BENCHMARK_NET = ipaddress.IPv4Network("198.18.0.0/15")  # RFC 2544 benchmarking, 131,072 addresses
_ip_numbers = itertools.count(1)


def new_test_ip() -> str:
    """
    Return an address that this test run has not returned before, from 198.18.0.0/15.

    Use this for every client IP a test invents. Do not write a random helper: a random draw from a
    small pool repeats (250 addresses repeated about 1 run in 250), and a repeated address is refused
    by the pair lock. The benchmarking range is used because random public-looking addresses include
    loopback, private and broadcast space, which trusted-proxy logic may treat differently later.
    """
    return str(_BENCHMARK_NET[next(_ip_numbers)])


def flush_test_valkey() -> None:
    """
    Empty the test Valkey database before each test, so counters from one test never reach another.
    The guard refuses any URL that does not name database 15, so no other database is ever flushed.
    """
    if not VALKEY_TEST_URL.endswith("/15"):
        raise RuntimeError(
            "the test Valkey URL must select database 15 (flushing is limited to it)"
        )
    client = redis.Redis.from_url(VALKEY_TEST_URL, socket_connect_timeout=2)
    try:
        client.flushdb()
    finally:
        client.close()


@dataclass(frozen=True)
class Accounts:
    handle: DbHandle
    seeded: Seeded
    admin_email: str
    employee_email: str
    password: str
    hmac_key: str
    rate_key: str
    key_path: Path


def _admin(handle: DbHandle, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(handle.admin_test_url()) as conn:
        rows = list(conn.execute(sql, params).fetchall())
        conn.commit()
    return rows


def _set(handle: DbHandle, sql: str, params: tuple[Any, ...]) -> None:
    with psycopg.connect(handle.admin_test_url()) as conn:
        conn.execute(sql, params)
        conn.commit()


@pytest.fixture
def accounts(
    grounded_db: DbHandle, seeded: Seeded, tmp_path: Path, monkeypatch: MonkeyPatch
) -> Iterator[Accounts]:
    password = secrets.token_urlsafe(16)
    for user in (seeded.a.admin, seeded.a.employee):
        _set(
            grounded_db,
            "UPDATE grounded.users SET password_hash = %s WHERE id = %s",
            (hash_password(password), user),
        )
    admin_email = _admin(
        grounded_db, "SELECT email FROM grounded.users WHERE id = %s", (seeded.a.admin,)
    )[0][0]
    employee_email = _admin(
        grounded_db, "SELECT email FROM grounded.users WHERE id = %s", (seeded.a.employee,)
    )[0][0]

    key_path = tmp_path / "jwt-keys.json"
    write_new_key_file(key_path)
    hmac_key = secrets.token_urlsafe(48)
    rate_key = secrets.token_urlsafe(48)
    flush_test_valkey()
    monkeypatch.setenv("DATABASE_URL", app_url(grounded_db))
    monkeypatch.setenv("REDIS_URL", VALKEY_TEST_URL)
    monkeypatch.setenv("AUTH_JWT_KEYS_FILE", str(key_path))
    monkeypatch.setenv("AUTH_LOG_HMAC_KEY", hmac_key)
    monkeypatch.setenv("AUTH_RATE_LIMIT_HMAC_KEY", rate_key)
    get_settings.cache_clear()

    yield Accounts(
        handle=grounded_db,
        seeded=seeded,
        admin_email=admin_email,
        employee_email=employee_email,
        password=password,
        hmac_key=hmac_key,
        rate_key=rate_key,
        key_path=key_path,
    )
    get_settings.cache_clear()
    _restore_tenant_a(grounded_db, seeded)


def _restore_tenant_a(handle: DbHandle, seeded: Seeded) -> None:
    """Undo any suspension, deactivation or status change a test made to tenant A's accounts."""
    _set(handle, "UPDATE grounded.tenants SET status = 'active' WHERE id = %s", (seeded.a.tenant,))
    _set(
        handle,
        "UPDATE grounded.tenant_memberships SET active = true WHERE tenant_id = %s",
        (seeded.a.tenant,),
    )
    _set(
        handle,
        "UPDATE grounded.users SET active = true WHERE id IN (%s, %s)",
        (seeded.a.admin, seeded.a.employee),
    )


@pytest.fixture
def client(accounts: Accounts) -> Iterator[TestClient]:
    with TestClient(
        create_app(), raise_server_exceptions=False, backend_options=SELECTOR
    ) as test_client:
        yield test_client


def _login(client: TestClient, email: str, password: str) -> Any:
    return client.post("/auth/login", json={"email": email, "password": password})


def _login_ok(client: TestClient, accounts: Accounts) -> tuple[str, str]:
    """Log in as tenant A's admin. Returns (access token, refresh cookie value)."""
    response = _login(client, accounts.admin_email, accounts.password)
    assert response.status_code == 200, response.text
    return response.json()["access_token"], response.cookies.get("__Secure-refresh", "")


def _ring(accounts: Accounts) -> KeyRing:
    return load_key_ring(accounts.key_path)

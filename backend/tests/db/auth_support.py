"""
Shared fixtures and helpers for the auth HTTP tests (ADR 0004).

Registered for every DB test in tests/db/conftest.py, so test modules can request `accounts` and
`client` without importing them. Helpers that tests call directly are imported explicitly.
"""

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
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


@dataclass(frozen=True)
class Accounts:
    handle: DbHandle
    seeded: Seeded
    admin_email: str
    employee_email: str
    password: str
    hmac_key: str
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
    monkeypatch.setenv("DATABASE_URL", app_url(grounded_db))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("AUTH_JWT_KEYS_FILE", str(key_path))
    monkeypatch.setenv("AUTH_LOG_HMAC_KEY", hmac_key)
    get_settings.cache_clear()

    yield Accounts(
        handle=grounded_db,
        seeded=seeded,
        admin_email=admin_email,
        employee_email=employee_email,
        password=password,
        hmac_key=hmac_key,
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

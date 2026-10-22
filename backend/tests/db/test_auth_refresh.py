"""
Refresh, rotation, reuse detection, logout and logout-all (ADR 0004, ADR 0013), against the real database.

The HTTP tests send the refresh cookie in the Cookie header themselves. httpx does not send a Secure
cookie back over plain http://testserver, so the cookie jar would hide the cookie from these tests.

The race test runs two separate sessions on two separate connections. The first request is paused
right after SELECT ... FOR UPDATE; the second request must be seen waiting for the lock in
pg_stat_activity before the first is released.
"""

import asyncio
import hashlib
import logging
import re
import secrets
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from grounded_answers import auth_refresh
from grounded_answers.auth_keys import load_key_ring
from grounded_answers.auth_refresh import RefreshInProgress, refresh
from grounded_answers.config import get_settings
from grounded_answers.database import create_session_factory
from tests.db.auth_support import Accounts, _admin, _login, _set
from tests.db.database import DbHandle
from tests.db.test_orm_models import app_url

pytestmark = pytest.mark.db

COOKIE_RE = re.compile(r"__Secure-refresh=([^;]+)")


def _origin() -> str:
    return get_settings().get_cors_origins()[0]


def _max_age(response: Any) -> int:
    match = re.search(r"Max-Age=(\d+)", response.headers.get("set-cookie", ""))
    assert match is not None, "the response must set a cookie with Max-Age"
    return int(match.group(1))


def _cookie_from(response: Any) -> str | None:
    match = COOKIE_RE.search(response.headers.get("set-cookie", ""))
    return match.group(1) if match else None


def _refresh_http(client: TestClient, raw: str | None, origin: str | None = None) -> Any:
    headers: dict[str, str] = {"Origin": _origin() if origin is None else origin}
    if raw is not None:
        headers["Cookie"] = f"__Secure-refresh={raw}"
    return client.post("/auth/refresh", headers=headers)


def _login_cookie(
    client: TestClient, accounts: Accounts, email: str | None = None
) -> tuple[str, str]:
    """Log in and return (access token, refresh cookie value)."""
    response = _login(client, email or accounts.admin_email, accounts.password)
    assert response.status_code == 200, response.text
    raw = _cookie_from(response)
    assert raw is not None
    return response.json()["access_token"], raw


def _digest(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("ascii")).digest()


def _family_of(handle: DbHandle, raw: str) -> uuid.UUID:
    digest = _digest(raw)
    rows = _admin(
        handle, "SELECT family_id FROM grounded.refresh_tokens WHERE token_hash = %s", (digest,)
    )
    assert rows, "the token must exist"
    family: uuid.UUID = rows[0][0]
    return family


def _family_rows(handle: DbHandle, family: uuid.UUID) -> list[tuple[Any, ...]]:
    return _admin(
        handle,
        "SELECT id, used_at, revoked_at, replaced_by, expires_at FROM grounded.refresh_tokens "
        "WHERE family_id = %s ORDER BY issued_at",
        (family,),
    )


def _token_row_id(handle: DbHandle, raw: str) -> uuid.UUID:
    digest = _digest(raw)
    rows = _admin(handle, "SELECT id FROM grounded.refresh_tokens WHERE token_hash = %s", (digest,))
    token_id: uuid.UUID = rows[0][0]
    return token_id


def _audit_count(accounts: Accounts, action: str) -> int:
    rows = _admin(
        accounts.handle,
        "SELECT count(*) FROM grounded.audit_log WHERE tenant_id = %s AND action = %s",
        (accounts.seeded.a.tenant, action),
    )
    return int(rows[0][0])


def _cleared(response: Any) -> bool:
    header = response.headers.get("set-cookie", "")
    return "__Secure-refresh=" in header and "Max-Age=0" in header


# --- rotation -----------------------------------------------------------------------------------


def test_rotation_chain_links_tokens_in_one_family(client: TestClient, accounts: Accounts) -> None:
    _, first = _login_cookie(client, accounts)
    second_response = _refresh_http(client, first)
    assert second_response.status_code == 200, second_response.text
    second = _cookie_from(second_response)
    assert second and second != first
    third_response = _refresh_http(client, second)
    assert third_response.status_code == 200
    third = _cookie_from(third_response)
    assert third

    family = _family_of(accounts.handle, first)
    rows = _family_rows(accounts.handle, family)
    assert len(rows) == 3
    first_row, second_row, third_row = rows
    assert first_row[1] is not None, "the first token is marked used"
    assert first_row[3] == second_row[0], "replaced_by links the rotation chain"
    assert second_row[3] == third_row[0]
    assert third_row[1] is None and third_row[2] is None, "the newest token is live"
    assert _family_of(accounts.handle, second) == family
    assert _family_of(accounts.handle, third) == family


def test_rotated_cookie_max_age_is_the_time_left_to_the_family_cap(
    client: TestClient, accounts: Accounts
) -> None:
    """D3B: a family starting 13 days ago has about one day left, not 14 days."""
    _, first = _login_cookie(client, accounts)
    family = _family_of(accounts.handle, first)
    _set(
        accounts.handle,
        "UPDATE grounded.refresh_tokens SET issued_at = now() - interval '13 days' WHERE family_id = %s",
        (family,),
    )
    response = _refresh_http(client, first)
    assert response.status_code == 200
    max_age = _max_age(response)
    assert 86400 - 120 <= max_age <= 86400 + 120


def test_fresh_family_gets_the_full_fourteen_days(client: TestClient, accounts: Accounts) -> None:
    _, first = _login_cookie(client, accounts)
    response = _refresh_http(client, first)
    max_age = _max_age(response)
    assert 14 * 86400 - 120 <= max_age <= 14 * 86400


# --- reuse and grace --------------------------------------------------------------------------------


def test_reuse_after_the_grace_window_revokes_the_family(
    client: TestClient, accounts: Accounts
) -> None:
    _, first = _login_cookie(client, accounts)
    second = _cookie_from(_refresh_http(client, first))
    assert second

    family = _family_of(accounts.handle, first)
    _set(
        accounts.handle,
        "UPDATE grounded.refresh_tokens SET used_at = now() - interval '11 seconds' "
        "WHERE token_hash = %s",
        (_digest(first),),
    )
    replay = _refresh_http(client, first)
    assert replay.status_code == 401
    assert replay.json() == {"detail": "Not authenticated"}
    assert _cleared(replay)

    newest = _refresh_http(client, second)
    assert newest.status_code == 401, "the family is revoked, so the newest token is dead too"
    assert all(row[2] is not None for row in _family_rows(accounts.handle, family))
    assert _audit_count(accounts, "refresh.reuse_detected") >= 1


def test_used_token_within_grace_returns_409_and_keeps_the_family(
    client: TestClient, accounts: Accounts
) -> None:
    _, first = _login_cookie(client, accounts)
    second = _cookie_from(_refresh_http(client, first))
    assert second

    replay = _refresh_http(client, first)
    assert replay.status_code == 409
    assert replay.json() == {"detail": "refresh_in_progress"}
    assert "set-cookie" not in replay.headers, "a 409 keeps the cookie the client already has"

    newest = _refresh_http(client, second)
    assert newest.status_code == 200, "no revocation happened inside the grace window"


# --- the forced race ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_two_concurrent_refreshes_with_one_token_rotate_exactly_once(
    accounts: Accounts, client: TestClient, monkeypatch: MonkeyPatch
) -> None:
    _, raw = _login_cookie(client, accounts)
    family = _family_of(accounts.handle, raw)
    ring = load_key_ring(accounts.key_path)
    engine: AsyncEngine = create_async_engine(
        app_url(accounts.handle), pool_size=2, max_overflow=0, pool_pre_ping=True
    )
    factory = create_session_factory(engine)

    locked = asyncio.Event()
    release = asyncio.Event()
    original_lock = auth_refresh._lock_token

    async def paused_lock(session: Any, token_id: uuid.UUID) -> Any:
        row = await original_lock(session, token_id)
        if not locked.is_set():
            # Request 1 holds the row lock here, inside its transaction, until the test releases it.
            locked.set()
            await release.wait()
        return row

    monkeypatch.setattr(auth_refresh, "_lock_token", paused_lock)

    def waiting_on_lock() -> int:
        rows = _admin(
            accounts.handle,
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND position('FOR UPDATE' in query) > 0",
        )
        return int(rows[0][0])

    async def rotate() -> Any:
        return await refresh(
            session_factory=factory,
            ring=ring,
            raw_refresh=raw,
            hmac_key=accounts.hmac_key.encode("ascii"),
            client_ip="203.0.113.7",
        )

    try:
        first = asyncio.create_task(rotate())
        await asyncio.wait_for(locked.wait(), timeout=10)

        second = asyncio.create_task(rotate())
        blocked = False
        for _ in range(100):
            await asyncio.sleep(0.05)
            if waiting_on_lock() >= 1:
                blocked = True
                break
        assert blocked, "request 2 must wait for the row lock held by request 1"
        assert not second.done(), "request 2 must not finish while request 1 holds the lock"

        release.set()
        result = await asyncio.wait_for(first, timeout=10)
        assert result.refresh_token

        with pytest.raises(RefreshInProgress):
            await asyncio.wait_for(second, timeout=10)
    finally:
        release.set()
        await engine.dispose()

    assert len(_family_rows(accounts.handle, family)) == 2, "exactly one new token was issued"


# --- expiry, revocation, failure cookies ----------------------------------------------------------------


def test_expired_token_is_refused_and_the_cookie_is_cleared(
    client: TestClient, accounts: Accounts
) -> None:
    _, raw = _login_cookie(client, accounts)
    _set(
        accounts.handle,
        "UPDATE grounded.refresh_tokens SET issued_at = now() - interval '2 hours', expires_at = now() - interval '1 minute' WHERE id = %s",
        (_token_row_id(accounts.handle, raw),),
    )
    response = _refresh_http(client, raw)
    assert response.status_code == 401
    assert _cleared(response)


def test_revoked_family_is_refused_and_the_cookie_is_cleared(
    client: TestClient, accounts: Accounts
) -> None:
    _, raw = _login_cookie(client, accounts)
    assert _refresh_http(client, raw) is not None
    logout = client.post(
        "/auth/logout", headers={"Origin": _origin(), "Cookie": f"__Secure-refresh={raw}"}
    )
    assert logout.status_code == 204
    response = _refresh_http(client, raw)
    assert response.status_code == 401
    assert _cleared(response)


def test_unknown_cookie_value_is_refused_and_cleared(
    client: TestClient, accounts: Accounts
) -> None:
    response = _refresh_http(client, secrets.token_urlsafe(32))
    assert response.status_code == 401
    assert _cleared(response)


def test_missing_cookie_is_refused(client: TestClient, accounts: Accounts) -> None:
    response = _refresh_http(client, None)
    assert response.status_code == 401


# --- logout and logout-all -----------------------------------------------------------------------------


def test_logout_revokes_the_family_clears_the_cookie_and_is_idempotent(
    client: TestClient, accounts: Accounts
) -> None:
    _, raw = _login_cookie(client, accounts)
    origin = _origin()
    first = client.post(
        "/auth/logout", headers={"Origin": origin, "Cookie": f"__Secure-refresh={raw}"}
    )
    assert first.status_code == 204
    assert _cleared(first)
    family = _family_of(accounts.handle, raw)
    assert all(row[2] is not None for row in _family_rows(accounts.handle, family))

    again = client.post(
        "/auth/logout", headers={"Origin": origin, "Cookie": f"__Secure-refresh={raw}"}
    )
    assert again.status_code == 204
    none = client.post("/auth/logout", headers={"Origin": origin})
    assert none.status_code == 204
    assert _refresh_http(client, raw).status_code == 401


def test_logout_all_revokes_only_this_users_families_in_this_tenant(
    client: TestClient, accounts: Accounts
) -> None:
    admin_access, admin_one = _login_cookie(client, accounts)
    _, admin_two = _login_cookie(client, accounts)
    _, employee_raw = _login_cookie(client, accounts, accounts.employee_email)

    response = client.post("/auth/logout-all", headers={"Authorization": f"Bearer {admin_access}"})
    assert response.status_code == 204
    assert _cleared(response)

    for raw in (admin_one, admin_two):
        family = _family_of(accounts.handle, raw)
        assert all(row[2] is not None for row in _family_rows(accounts.handle, family))
    assert _refresh_http(client, employee_raw).status_code == 200, (
        "another user's family is untouched"
    )

    again = client.post("/auth/logout-all", headers={"Authorization": f"Bearer {admin_access}"})
    assert again.status_code == 204, "a second call changes nothing and still succeeds"


def test_logout_all_is_idempotent_for_a_fresh_access_token(
    client: TestClient, accounts: Accounts
) -> None:
    access, _ = _login_cookie(client, accounts)
    assert (
        client.post("/auth/logout-all", headers={"Authorization": f"Bearer {access}"}).status_code
        == 204
    )
    assert (
        client.post("/auth/logout-all", headers={"Authorization": f"Bearer {access}"}).status_code
        == 204
    )


# --- per-request checks on refresh --------------------------------------------------------------------


def test_suspended_tenant_on_refresh_is_403_revokes_the_family_and_audits(
    client: TestClient, accounts: Accounts
) -> None:
    _, raw = _login_cookie(client, accounts)
    _set(
        accounts.handle,
        "UPDATE grounded.tenants SET status = 'suspended' WHERE id = %s",
        (accounts.seeded.a.tenant,),
    )
    response = _refresh_http(client, raw)
    assert response.status_code == 403
    assert response.json() == {"detail": "Tenant access is suspended"}
    assert _cleared(response)
    family = _family_of(accounts.handle, raw)
    assert all(row[2] is not None for row in _family_rows(accounts.handle, family))
    assert _audit_count(accounts, "refresh.refused") >= 1


def test_inactive_membership_on_refresh_is_401_and_revokes_the_family(
    client: TestClient, accounts: Accounts
) -> None:
    _, raw = _login_cookie(client, accounts)
    _set(
        accounts.handle,
        "UPDATE grounded.tenant_memberships SET active = false WHERE user_id = %s",
        (accounts.seeded.a.admin,),
    )
    response = _refresh_http(client, raw)
    assert response.status_code == 401
    family = _family_of(accounts.handle, raw)
    assert all(row[2] is not None for row in _family_rows(accounts.handle, family))


# --- origin (D6B) ------------------------------------------------------------------------------------


def test_refresh_and_logout_refuse_a_missing_or_foreign_origin(
    client: TestClient, accounts: Accounts
) -> None:
    _, raw = _login_cookie(client, accounts)
    no_origin = client.post("/auth/refresh", headers={"Cookie": f"__Secure-refresh={raw}"})
    foreign = _refresh_http(client, raw, origin="https://evil.example")
    logout_foreign = client.post(
        "/auth/logout",
        headers={"Origin": "https://evil.example", "Cookie": f"__Secure-refresh={raw}"},
    )
    assert no_origin.status_code == 403
    assert foreign.status_code == 403
    assert logout_foreign.status_code == 403
    assert _refresh_http(client, raw).status_code == 200, (
        "the refused requests did not spend the token"
    )


# --- logs and audit ------------------------------------------------------------------------------------


def test_no_tokens_or_hashes_appear_in_the_logs(
    client: TestClient, accounts: Accounts, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="grounded_answers"):
        access, raw = _login_cookie(client, accounts)
        rotated = _cookie_from(_refresh_http(client, raw))
        _refresh_http(client, raw)  # grace 409
        client.post(
            "/auth/logout", headers={"Origin": _origin(), "Cookie": f"__Secure-refresh={rotated}"}
        )
    text_log = caplog.text
    assert rotated is not None
    for secret in (access, raw, rotated):
        assert secret not in text_log
    assert _digest(rotated or "").hex() not in text_log
    assert accounts.password not in text_log

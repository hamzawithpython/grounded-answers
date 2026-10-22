"""
Login, token checks and per-request checks over HTTP, against the real database (ADR 0004).

The app is built with create_app() and started through its real lifespan, so the key file, the
HMAC key and the dummy hash are loaded the same way production loads them. Accounts get known
passwords through the cluster admin connection. Each test restores the seeded state it changed.
"""

import hashlib
import hmac
import logging
import secrets
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from grounded_answers import auth_service
from grounded_answers.auth_keys import KeyRing, load_key_ring, write_new_key_file
from grounded_answers.auth_tokens import AUDIENCE, ISSUER
from grounded_answers.config import get_settings
from grounded_answers.main import create_app
from grounded_answers.passwords import hash_password
from grounded_answers.passwords import verify_password as real_verify_password
from grounded_answers.runtime import selector_loop_factory
from tests.db.database import DbHandle
from tests.db.seed import Seeded
from tests.db.test_orm_models import app_url

pytestmark = pytest.mark.db

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


def _forge(ring: KeyRing, *, kid: str | None = None, **claims: Any) -> str:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "sub": "",
        "tid": "",
        "role": "employee",
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "jti": str(uuid.uuid4()),
    }
    base.update(claims)
    return jwt.encode(
        base, ring.signing_key, algorithm="EdDSA", headers={"kid": kid or ring.active_kid}
    )


def _admin_claims(accounts: Accounts) -> dict[str, str]:
    return {"sub": str(accounts.seeded.a.admin), "tid": str(accounts.seeded.a.tenant)}


def _audit(accounts: Accounts, action: str) -> list[tuple[Any, ...]]:
    return _admin(
        accounts.handle,
        "SELECT actor_user_id, detail::text, ip_hmac FROM grounded.audit_log "
        "WHERE tenant_id = %s AND action = %s ORDER BY created_at",
        (accounts.seeded.a.tenant, action),
    )


# --- success path -----------------------------------------------------------------------------


def test_login_issues_an_access_token_and_a_hardened_refresh_cookie(
    client: TestClient, accounts: Accounts
) -> None:
    response = _login(client, accounts.admin_email, accounts.password)
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"  # noqa: S105 - OAuth token type label, not a secret
    assert body["expires_in"] == 900
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert claims["sub"] == str(accounts.seeded.a.admin)

    set_cookie = response.headers["set-cookie"].lower()
    assert "__secure-refresh=" in set_cookie
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/auth" in set_cookie
    assert "domain=" not in set_cookie


def test_refresh_token_is_stored_as_a_hash_only(client: TestClient, accounts: Accounts) -> None:
    _, raw = _login_ok(client, accounts)
    assert raw
    rows = _admin(
        accounts.handle,
        "SELECT token_hash FROM grounded.refresh_tokens WHERE user_id = %s ORDER BY issued_at DESC LIMIT 1",
        (accounts.seeded.a.admin,),
    )
    assert rows[0][0] == hashlib.sha256(raw.encode("ascii")).digest()
    assert raw.encode("ascii") not in bytes(rows[0][0])


def test_me_returns_the_role_from_the_database_not_the_token(
    client: TestClient, accounts: Accounts
) -> None:
    """A token that claims 'employee' for an admin still gets the database role."""
    token = _forge(_ring(accounts), **_admin_claims(accounts), role="employee")
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["role"] == "tenant_admin"


def test_protected_route_without_a_token_is_refused_with_a_challenge(client: TestClient) -> None:
    response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}
    assert response.headers["www-authenticate"] == "Bearer"


# --- token rules over HTTP ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["expired", "wrong_audience", "wrong_issuer", "unknown_kid", "alg_none", "tampered"],
)
def test_bad_tokens_are_refused_with_the_generic_message(
    client: TestClient, accounts: Accounts, kind: str
) -> None:
    ring = _ring(accounts)
    claims = _admin_claims(accounts)
    if kind == "expired":
        token = _forge(
            ring,
            **claims,
            iat=datetime.now(UTC) - timedelta(hours=2),
            exp=datetime.now(UTC) - timedelta(hours=1),
        )
    elif kind == "wrong_audience":
        token = _forge(ring, **claims, aud="someone-else")
    elif kind == "wrong_issuer":
        token = _forge(ring, **claims, iss="someone-else")
    elif kind == "unknown_kid":
        token = _forge(ring, kid="ed25519-0000000000000000", **claims)
    elif kind == "alg_none":
        token = jwt.encode(
            {
                **claims,
                "iss": ISSUER,
                "aud": AUDIENCE,
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
                "jti": "x",
            },
            UNSIGNED_KEY,
            algorithm="none",
            headers={"kid": ring.active_kid},
        )
    else:
        good = _forge(ring, **claims)
        head, payload, signature = good.split(".")
        token = f"{head}.{payload}.{signature[:-1]}{'A' if signature[-1] != 'A' else 'B'}"
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


def test_forged_tenant_id_is_refused(
    client: TestClient, accounts: Accounts, seeded_tenant_b: uuid.UUID
) -> None:
    """A valid signature with another tenant's id: RLS hides the membership, so 401."""
    token = _forge(_ring(accounts), sub=str(accounts.seeded.a.admin), tid=str(seeded_tenant_b))
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.fixture
def seeded_tenant_b(accounts: Accounts) -> uuid.UUID:
    return accounts.seeded.b.tenant


# --- per-request checks ---------------------------------------------------------------------------


def test_suspended_tenant_is_refused_per_request_with_403(
    client: TestClient, accounts: Accounts
) -> None:
    token, _ = _login_ok(client, accounts)
    _set(
        accounts.handle,
        "UPDATE grounded.tenants SET status = 'suspended' WHERE id = %s",
        (accounts.seeded.a.tenant,),
    )
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json() == {"detail": "Tenant access is suspended"}


def test_inactive_membership_is_refused_per_request(client: TestClient, accounts: Accounts) -> None:
    token, _ = _login_ok(client, accounts)
    _set(
        accounts.handle,
        "UPDATE grounded.tenant_memberships SET active = false WHERE user_id = %s",
        (accounts.seeded.a.admin,),
    )
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_inactive_user_is_refused_per_request(client: TestClient, accounts: Accounts) -> None:
    token, _ = _login_ok(client, accounts)
    _set(
        accounts.handle,
        "UPDATE grounded.users SET active = false WHERE id = %s",
        (accounts.seeded.a.admin,),
    )
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


# --- login failures: generic, audited, timed the same ----------------------------------------------


def test_wrong_password_is_generic_and_audited(client: TestClient, accounts: Accounts) -> None:
    response = _login(client, accounts.admin_email, "not-the-password-at-all")
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password"}
    rows = _audit(accounts, "login.failure")
    assert rows, "a failed login for a known user must be audited"
    assert rows[-1][0] == accounts.seeded.a.admin
    assert '"reason": "password"' in rows[-1][1]


def test_unknown_email_is_generic_and_still_runs_the_hash(
    client: TestClient, accounts: Accounts, monkeypatch: MonkeyPatch
) -> None:
    calls: list[str] = []

    def counting(stored_hash: str, password: str) -> bool:
        calls.append(stored_hash[:10])
        return real_verify_password(stored_hash, password)

    monkeypatch.setattr(auth_service, "verify_password", counting)
    response = _login(client, "nobody-here@indus.test", accounts.password)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password"}
    assert len(calls) == 1, "unknown emails must be verified against the dummy hash"


def test_every_login_failure_has_a_byte_identical_body(
    client: TestClient, accounts: Accounts
) -> None:
    """Unknown email, wrong password, suspended tenant, inactive membership, inactive user."""
    responses = [
        _login(client, "nobody-here@indus.test", accounts.password),
        _login(client, accounts.admin_email, "wrong-password-x"),
    ]
    _set(
        accounts.handle,
        "UPDATE grounded.tenants SET status = 'suspended' WHERE id = %s",
        (accounts.seeded.a.tenant,),
    )
    responses.append(_login(client, accounts.admin_email, accounts.password))
    _set(
        accounts.handle,
        "UPDATE grounded.tenants SET status = 'active' WHERE id = %s",
        (accounts.seeded.a.tenant,),
    )
    _set(
        accounts.handle,
        "UPDATE grounded.tenant_memberships SET active = false WHERE user_id = %s",
        (accounts.seeded.a.admin,),
    )
    responses.append(_login(client, accounts.admin_email, accounts.password))
    _set(
        accounts.handle,
        "UPDATE grounded.tenant_memberships SET active = true WHERE user_id = %s",
        (accounts.seeded.a.admin,),
    )
    _set(
        accounts.handle,
        "UPDATE grounded.users SET active = false WHERE id = %s",
        (accounts.seeded.a.admin,),
    )
    responses.append(_login(client, accounts.admin_email, accounts.password))
    _set(
        accounts.handle,
        "UPDATE grounded.users SET active = true WHERE id = %s",
        (accounts.seeded.a.admin,),
    )

    statuses = {r.status_code for r in responses}
    contents = {r.content for r in responses}
    cookies = {bool(r.cookies.get("__Secure-refresh")) for r in responses}
    assert statuses == {401}
    assert contents == {b'{"detail":"Invalid email or password"}'}
    assert cookies == {False}


def test_successful_login_writes_an_audit_row_with_a_keyed_ip(
    client: TestClient, accounts: Accounts
) -> None:
    _login_ok(client, accounts)
    rows = _audit(accounts, "login.success")
    assert rows
    actor, detail, ip_hmac = rows[-1]
    assert actor == accounts.seeded.a.admin
    assert detail is None
    expected = hmac.new(accounts.hmac_key.encode("ascii"), b"testclient", hashlib.sha256).digest()
    assert bytes(ip_hmac) == expected
    assert bytes(ip_hmac) != b"testclient"


def test_password_check_runs_in_a_worker_thread_not_on_the_event_loop(
    client: TestClient, accounts: Accounts, monkeypatch: MonkeyPatch
) -> None:
    seen: dict[str, int] = {}
    original_lookup = auth_service._lookup

    async def record_loop(*args: Any, **kwargs: Any) -> Any:
        seen["loop"] = threading.get_ident()
        return await original_lookup(*args, **kwargs)

    def record_verify(stored_hash: str, password: str) -> bool:
        seen["verify"] = threading.get_ident()
        return real_verify_password(stored_hash, password)

    monkeypatch.setattr(auth_service, "_lookup", record_loop)
    monkeypatch.setattr(auth_service, "verify_password", record_verify)
    _login(client, accounts.admin_email, accounts.password)
    assert seen["verify"] != seen["loop"]


def test_validation_error_never_echoes_the_password(
    client: TestClient, accounts: Accounts, caplog: pytest.LogCaptureFixture
) -> None:
    too_long = LOGIN_BODY_MARKER + "x" * 300
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/auth/login", json={"email": accounts.admin_email, "password": too_long}
        )
    assert response.status_code == 422
    assert LOGIN_BODY_MARKER not in response.text
    assert LOGIN_BODY_MARKER not in caplog.text
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "password"]
    assert set(detail[0]) == {"type", "loc", "msg"}


def test_logs_never_contain_passwords_tokens_emails_or_raw_ips(
    client: TestClient, accounts: Accounts, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="grounded_answers"):
        token, raw_refresh = _login_ok(client, accounts)
        _login(client, accounts.admin_email, "wrong-password-logged")
        _login(client, "nobody-here@indus.test", accounts.password)
        client.get("/auth/me", headers={"Authorization": "Bearer not-a-token"})
    text = caplog.text
    assert accounts.password not in text
    assert token not in text
    assert raw_refresh not in text
    assert accounts.admin_email not in text
    assert "nobody-here@indus.test" not in text
    assert "wrong-password-logged" not in text
    assert "testclient" not in text
    assert "login.failed" in text or "access.rejected" in text

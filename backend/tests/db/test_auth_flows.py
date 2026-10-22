"""
Login, token checks and per-request checks over HTTP, against the real database (ADR 0004).

The app is built with create_app() and started through its real lifespan, so the key file, the
HMAC key and the dummy hash are loaded the same way production loads them. Accounts get known
passwords through the cluster admin connection. Each test restores the seeded state it changed.
"""

import hashlib
import hmac
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from grounded_answers import auth_service
from grounded_answers.auth_keys import KeyRing
from grounded_answers.auth_tokens import AUDIENCE, ISSUER
from grounded_answers.passwords import verify_password as real_verify_password

pytestmark = pytest.mark.db

from tests.db.auth_support import (  # noqa: E402
    LOGIN_BODY_MARKER,
    UNSIGNED_KEY,
    Accounts,
    _admin,
    _login,
    _login_ok,
    _ring,
    _set,
)


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
        middle = len(signature) // 2
        token = f"{head}.{payload}.{signature[:middle]}{'A' if signature[middle] != 'A' else 'B'}{signature[middle + 1 :]}"
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

"""
`authenticated_principal` refuses exactly what `AuthenticatedRequest` refuses (ADR 0033, D1; the review of the 5c plan).

POST /ask uses the lighter dependency so that no database connection is held while a model runs. That is only safe if
it is not a weaker check. Every case below is sent to GET /auth/me (which uses `AuthenticatedRequest`) and to a test
route added to the running app that uses `authenticated_principal`, and the two answers must be identical: status,
body and the WWW-Authenticate header. For the cases that succeed, the principal (user, tenant, role) must match too.

The cases are the ones the plan listed: no or malformed header, an expired token, a wrong key, a wrong audience or
issuer, an unknown key id, `alg: none`, a tampered signature, a forged tenant id, a suspended tenant, an inactive
membership, an inactive user, a user who is not a member of the tenant, and a role changed in the database.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from grounded_answers.auth_api import AuthenticatedPrincipal, AuthenticatedRequest
from grounded_answers.auth_keys import KeyRing, load_key_ring, write_new_key_file
from grounded_answers.auth_tokens import AUDIENCE, ISSUER, issue_access_token
from tests.db.auth_support import Accounts, _admin, _set

pytestmark = pytest.mark.db


def _forge(ring: KeyRing, *, kid: str | None = None, **claims: Any) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": "",
        "tid": "",
        "role": "employee",
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "jti": str(uuid.uuid4()),
    }
    payload.update(claims)
    return jwt.encode(
        payload, ring.signing_key, algorithm="EdDSA", headers={"kid": kid or ring.active_kid}
    )


@pytest.fixture
def both(client: TestClient) -> TestClient:
    """The running app with a second route behind the lighter dependency."""

    async def principal_route(principal: AuthenticatedPrincipal) -> dict[str, str]:
        return {
            "user_id": str(principal.user_id),
            "tenant_id": str(principal.tenant_id),
            "role": principal.role,
        }

    client.app.add_api_route("/_parity/principal", principal_route, methods=["GET"])  # type: ignore[attr-defined]
    return client


def _ask_both(client: TestClient, headers: dict[str, str]) -> tuple[Any, Any]:
    return client.get("/auth/me", headers=headers), client.get(
        "/_parity/principal", headers=headers
    )


def _same(client: TestClient, headers: dict[str, str]) -> Any:
    old, new = _ask_both(client, headers)
    assert (new.status_code, new.json()) == (old.status_code, old.json())
    assert new.headers.get("www-authenticate") == old.headers.get("www-authenticate")
    return new


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _good_token(accounts: Accounts) -> str:
    ring = load_key_ring(accounts.key_path)
    return issue_access_token(
        ring=ring,
        user_id=accounts.seeded.a.admin,
        tenant_id=accounts.seeded.a.tenant,
        role_hint="tenant_admin",
    )


# ---- header and token cases ----


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer a b"},
        {"Authorization": "bearer not-a-token"},
        {"Authorization": "Bearer not.a.token"},
    ],
)
def test_malformed_headers_are_refused_identically(
    both: TestClient, headers: dict[str, str]
) -> None:
    refused = _same(both, headers)
    assert refused.status_code == 401
    assert refused.json() == {"detail": "Not authenticated"}
    assert refused.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "kind",
    [
        "expired",
        "wrong_audience",
        "wrong_issuer",
        "unknown_kid",
        "alg_none",
        "tampered",
        "other_key",
        "forged_tenant",
        "not_a_member",
    ],
)
def test_bad_tokens_are_refused_identically(
    both: TestClient, accounts: Accounts, tmp_path: Path, kind: str
) -> None:
    ring = load_key_ring(accounts.key_path)
    claims = {"sub": str(accounts.seeded.a.admin), "tid": str(accounts.seeded.a.tenant)}
    builders: dict[str, Callable[[], str]] = {
        "expired": lambda: _forge(
            ring,
            **claims,
            iat=datetime.now(UTC) - timedelta(hours=2),
            exp=datetime.now(UTC) - timedelta(hours=1),
        ),
        "wrong_audience": lambda: _forge(ring, **claims, aud="someone-else"),
        "wrong_issuer": lambda: _forge(ring, **claims, iss="someone-else"),
        "unknown_kid": lambda: _forge(ring, kid="ed25519-0000000000000000", **claims),
        "alg_none": lambda: jwt.encode(
            {
                **claims,
                "iss": ISSUER,
                "aud": AUDIENCE,
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
                "jti": "x",
            },
            None,  # type: ignore[arg-type]
            algorithm="none",
            headers={"kid": ring.active_kid},
        ),
        "tampered": lambda: _tamper(_forge(ring, **claims)),
        "other_key": lambda: _signed_by_another_ring(tmp_path, accounts),
        # a valid signature naming tenant B for tenant A's admin: row-level security hides the membership
        "forged_tenant": lambda: _forge(
            ring, sub=str(accounts.seeded.a.admin), tid=str(accounts.seeded.b.tenant)
        ),
        # tenant B's user in tenant A: a valid account, but not a member of the tenant in the token
        "not_a_member": lambda: _forge(
            ring, sub=str(accounts.seeded.b.admin), tid=str(accounts.seeded.a.tenant)
        ),
    }
    refused = _same(both, _bearer(builders[kind]()))
    assert refused.status_code == 401
    assert refused.json() == {"detail": "Not authenticated"}


def _tamper(token: str) -> str:
    head, payload, signature = token.split(".")
    middle = len(signature) // 2
    flipped = "A" if signature[middle] != "A" else "B"
    return f"{head}.{payload}.{signature[:middle]}{flipped}{signature[middle + 1 :]}"


def _signed_by_another_ring(tmp_path: Path, accounts: Accounts) -> str:
    other_file = tmp_path / "other-keys.json"
    write_new_key_file(other_file)
    other = load_key_ring(other_file)
    return issue_access_token(
        ring=other,
        user_id=accounts.seeded.a.admin,
        tenant_id=accounts.seeded.a.tenant,
        role_hint="tenant_admin",
    )


# ---- changes in the database after the token was issued ----


def test_a_suspended_tenant_is_refused_identically(both: TestClient, accounts: Accounts) -> None:
    token = _good_token(accounts)
    _set(
        accounts.handle,
        "UPDATE grounded.tenants SET status = 'suspended' WHERE id = %s",
        (accounts.seeded.a.tenant,),
    )
    refused = _same(both, _bearer(token))
    assert refused.status_code == 403
    assert refused.json() == {"detail": "Tenant access is suspended"}


def test_an_inactive_membership_is_refused_identically(
    both: TestClient, accounts: Accounts
) -> None:
    token = _good_token(accounts)
    _set(
        accounts.handle,
        "UPDATE grounded.tenant_memberships SET active = false WHERE user_id = %s",
        (accounts.seeded.a.admin,),
    )
    assert _same(both, _bearer(token)).status_code == 401


def test_an_inactive_user_is_refused_identically(both: TestClient, accounts: Accounts) -> None:
    token = _good_token(accounts)
    _set(
        accounts.handle,
        "UPDATE grounded.users SET active = false WHERE id = %s",
        (accounts.seeded.a.admin,),
    )
    assert _same(both, _bearer(token)).status_code == 401


# ---- the cases that succeed: the same principal, with the role from the database ----


def test_a_good_token_gives_the_same_principal(both: TestClient, accounts: Accounts) -> None:
    ok = _same(both, _bearer(_good_token(accounts)))
    assert ok.status_code == 200
    assert ok.json()["role"] == "tenant_admin"
    assert ok.json()["user_id"] == str(accounts.seeded.a.admin)
    assert ok.json()["tenant_id"] == str(accounts.seeded.a.tenant)


def test_the_role_comes_from_the_database_not_the_token(
    both: TestClient, accounts: Accounts
) -> None:
    ring = load_key_ring(accounts.key_path)
    lying = _forge(
        ring,
        sub=str(accounts.seeded.a.admin),
        tid=str(accounts.seeded.a.tenant),
        role="employee",  # the token says employee; the database says tenant_admin
    )
    assert _same(both, _bearer(lying)).json()["role"] == "tenant_admin"


def test_a_role_changed_in_the_database_is_seen_at_once(
    both: TestClient, accounts: Accounts
) -> None:
    token = _good_token(accounts)
    _set(
        accounts.handle,
        "UPDATE grounded.tenant_memberships SET role = 'employee' WHERE user_id = %s AND tenant_id = %s",
        (accounts.seeded.a.admin, accounts.seeded.a.tenant),
    )
    try:
        assert _same(both, _bearer(token)).json()["role"] == "employee"
    finally:
        _set(
            accounts.handle,
            "UPDATE grounded.tenant_memberships SET role = 'tenant_admin' WHERE user_id = %s AND tenant_id = %s",
            (accounts.seeded.a.admin, accounts.seeded.a.tenant),
        )
    assert _admin(
        accounts.handle,
        "SELECT role FROM grounded.tenant_memberships WHERE user_id = %s AND tenant_id = %s",
        (accounts.seeded.a.admin, accounts.seeded.a.tenant),
    ) == [("tenant_admin",)]


# ---- the lighter dependency really does release its connection ----


def test_the_principal_dependency_leaves_no_transaction_open_while_the_route_runs(
    client: TestClient, accounts: Accounts
) -> None:
    seen: list[list[tuple[Any, ...]]] = []

    async def slow_route(principal: AuthenticatedPrincipal) -> dict[str, str]:
        seen.append(
            _admin(
                accounts.handle,
                "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_user' "
                "AND state IN ('idle in transaction', 'idle in transaction (aborted)')",
            )
        )
        return {"role": principal.role}

    client.app.add_api_route("/_parity/slow", slow_route, methods=["GET"])  # type: ignore[attr-defined]
    response = client.get("/_parity/slow", headers=_bearer(_good_token(accounts)))
    assert response.status_code == 200
    assert seen == [[(0,)]]


def test_the_old_dependency_does_hold_a_transaction_so_the_measurement_can_fail(
    client: TestClient, accounts: Accounts
) -> None:
    """The contrast: with AuthenticatedRequest the same probe sees an open transaction, so the test above means something."""
    seen: list[int] = []

    async def holding_route(ctx: AuthenticatedRequest) -> dict[str, str]:
        rows = _admin(
            accounts.handle,
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_user' "
            "AND state IN ('idle in transaction', 'idle in transaction (aborted)')",
        )
        seen.append(int(rows[0][0]))
        return {"role": ctx.principal.role}

    client.app.add_api_route("/_parity/holding", holding_route, methods=["GET"])  # type: ignore[attr-defined]
    assert client.get("/_parity/holding", headers=_bearer(_good_token(accounts))).status_code == 200
    assert seen and seen[0] >= 1

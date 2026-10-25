"""GET /documents over HTTP against PostgreSQL (ADR 0029).

The documents get explicit created_at values, so the expected order is known. A document added between two
requests is created now, so it sorts above every cursor and must never repeat or displace an item on a later page.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from grounded_answers.ingest.failures import FAILURE_REASONS

from .auth_support import Accounts
from .ingest_support import Made, make_version
from .seed import Seeded

pytestmark = pytest.mark.db

MARKDOWN = "text/markdown"
BASE_TIME = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def list_made(made: Made, admin: psycopg.Connection) -> Iterator[Made]:
    """The documents the test creates, and the audit rows its deletes write, are removed afterwards."""
    yield made
    for document in made.documents:
        admin.execute(
            "DELETE FROM grounded.audit_log WHERE action = 'document.delete' AND target_id = %s",
            (document,),
        )


def _token(client: TestClient, email: str, password: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


def _listing(client: TestClient, token: str, **params: Any) -> Any:
    return client.get("/documents", params=params, headers={"Authorization": f"Bearer {token}"})


MAX_WALK_PAGES = 100


def _walk(client: TestClient, token: str, limit: int, cursor: str | None) -> list[str]:
    """Follow next_cursor to the end, from cursor (None: the first page). Returns every id in order.

    Bounded: a cursor that does not move forward fails here with a message, rather than hanging the suite."""
    ids: list[str] = []
    for _ in range(MAX_WALK_PAGES):
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = _listing(client, token, **params)
        assert response.status_code == 200, response.text
        body = response.json()
        ids.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return ids
    raise AssertionError("the walk did not end: next_cursor does not move forward")


def _all_pages(client: TestClient, token: str, limit: int) -> list[str]:
    return _walk(client, token, limit, None)


def _live_document(
    admin: psycopg.Connection,
    list_made: Made,
    seeded: Seeded,
    *,
    title: str,
    created: datetime,
    tenant: uuid.UUID | None = None,
    creator: uuid.UUID | None = None,
    status: str = "ready",
    version_no: int = 1,
    failure_reason: str | None = None,
) -> uuid.UUID:
    document, version = make_version(
        admin,
        list_made,
        tenant or seeded.a.tenant,
        creator or seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status=status,
        title=title,
        version_no=version_no,
    )
    admin.execute(
        "UPDATE grounded.documents SET created_at = %s WHERE id = %s", (created, document)
    )
    if failure_reason is not None:
        admin.execute(
            "UPDATE grounded.document_versions SET failure_reason = %s WHERE id = %s",
            (failure_reason, version),
        )
    if status == "ready":
        admin.execute(
            "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s",
            (version, document),
        )
    return document


@pytest.fixture
def admin_token(client: TestClient, accounts: Accounts) -> str:
    return _token(client, accounts.admin_email, accounts.password)


def test_pages_cover_every_document_once_in_newest_first_order(
    client: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    list_made: Made,
    admin_token: str,
) -> None:
    seeded = accounts.seeded
    created = [
        _live_document(
            admin, list_made, seeded, title=f"Doc {i}", created=BASE_TIME + timedelta(minutes=i)
        )
        for i in range(5)
    ]
    expected = [str(document) for document in reversed(created)]

    ids = _all_pages(client, admin_token, limit=2)

    assert len(ids) == len(set(ids)), "no document may repeat across pages"
    assert ids == [str(d) for d in _live_ids(admin, seeded)], "every live document, newest first"
    assert set(expected) <= set(ids)
    assert ids.index(expected[0]) < ids.index(expected[-1]), (
        "the newest created document comes first"
    )


def test_a_document_added_between_pages_repeats_and_skips_nothing(
    client: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    list_made: Made,
    admin_token: str,
) -> None:
    seeded = accounts.seeded
    for i in range(5):
        _live_document(
            admin, list_made, seeded, title=f"Doc {i}", created=BASE_TIME + timedelta(minutes=i)
        )

    first = _listing(client, admin_token, limit=2).json()
    before_second = [item["id"] for item in first["items"]]
    newcomer = _live_document(
        admin, list_made, seeded, title="Added between pages", created=datetime.now(UTC)
    )
    rest = _all_pages_from(client, admin_token, first["next_cursor"], limit=2)

    assert str(newcomer) not in rest, (
        "a document added after the cursor belongs above it, not on a later page"
    )
    everything = before_second + rest
    assert len(everything) == len(set(everything)), "no repeats across the whole walk"
    assert everything == [str(d) for d in _live_ids(admin, seeded) if d != newcomer], (
        "no gaps either"
    )


def _all_pages_from(client: TestClient, token: str, cursor: str | None, limit: int) -> list[str]:
    return _walk(client, token, limit, cursor)


def _live_ids(admin: psycopg.Connection, seeded: Seeded) -> list[uuid.UUID]:
    rows = admin.execute(
        "SELECT id FROM grounded.documents WHERE tenant_id = %s AND deleted_at IS NULL "
        "ORDER BY created_at DESC, id DESC",
        (seeded.a.tenant,),
    ).fetchall()
    return [row[0] for row in rows]


def test_a_deleted_document_is_excluded(
    client: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    list_made: Made,
    admin_token: str,
) -> None:
    seeded = accounts.seeded
    kept = _live_document(admin, list_made, seeded, title="Kept", created=BASE_TIME)
    gone = _live_document(
        admin, list_made, seeded, title="Gone", created=BASE_TIME + timedelta(minutes=1)
    )
    deleted = client.delete(
        f"/documents/{gone}", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert deleted.status_code == 204

    ids = _all_pages(client, admin_token, limit=50)

    assert str(gone) not in ids
    assert str(kept) in ids


def test_another_tenants_documents_never_appear(
    client: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    list_made: Made,
    admin_token: str,
) -> None:
    seeded = accounts.seeded
    foreign = _live_document(
        admin,
        list_made,
        seeded,
        title="Tenant B confidential title",
        created=datetime.now(UTC),
        tenant=seeded.b.tenant,
        creator=seeded.b.admin,
    )

    body = _listing(client, admin_token, limit=50).text

    assert str(foreign) not in body
    assert "Tenant B confidential title" not in body


def test_an_employee_is_refused(client: TestClient, accounts: Accounts) -> None:
    token = _token(client, accounts.employee_email, accounts.password)

    response = _listing(client, token)

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


def test_the_default_limit_is_twenty_and_a_bad_limit_is_refused(
    client: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    list_made: Made,
    admin_token: str,
) -> None:
    seeded = accounts.seeded
    for i in range(21):
        _live_document(
            admin, list_made, seeded, title=f"Doc {i}", created=BASE_TIME + timedelta(minutes=i)
        )

    first = _listing(client, admin_token).json()
    assert len(first["items"]) == 20
    assert first["next_cursor"] is not None

    for bad in ("0", "51", "abc"):
        response = _listing(client, admin_token, limit=bad)
        assert response.status_code == 400
        assert response.json() == {"detail": "limit must be a whole number from 1 to 50"}
    assert _listing(client, admin_token, limit=50).status_code == 200


def test_a_malformed_cursor_is_refused_with_a_generic_message(
    client: TestClient, accounts: Accounts, admin_token: str
) -> None:
    response = _listing(client, admin_token, cursor="SOME-ATTACKER-INPUT!!")

    assert response.status_code == 400
    assert response.json() == {"detail": "the cursor is not valid"}
    assert "SOME-ATTACKER-INPUT" not in response.text


def test_current_and_latest_versions_are_both_shown(
    client: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    list_made: Made,
    admin_token: str,
) -> None:
    seeded = accounts.seeded
    replaced = _live_document(
        admin, list_made, seeded, title="Replaced", created=BASE_TIME, status="ready"
    )
    # A failed replacement (version 2) on the same document. make_version creates its own document, so the new
    # version is moved onto the replaced one; the empty document it left behind is removed with the others.
    _, failed = make_version(
        admin,
        list_made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="failed",
        title="Replaced",
        version_no=2,
    )
    admin.execute(
        "UPDATE grounded.document_versions SET document_id = %s, failure_reason = 'embedding' WHERE id = %s",
        (replaced, failed),
    )
    queued_doc = _live_document(
        admin,
        list_made,
        seeded,
        title="Queued only",
        created=BASE_TIME + timedelta(minutes=5),
        status="queued",
    )

    items = {item["id"]: item for item in _listing(client, admin_token, limit=50).json()["items"]}

    replaced_item = items[str(replaced)]
    assert replaced_item["current_version_status"] == "ready"
    assert replaced_item["latest_version_no"] == 2
    assert replaced_item["latest_version_status"] == "failed"
    assert replaced_item["latest_failure_reason"] == "embedding"
    queued_item = items[str(queued_doc)]
    assert queued_item["current_version_status"] is None
    assert queued_item["latest_version_status"] == "queued"
    assert queued_item["latest_failure_reason"] is None


@pytest.mark.parametrize("reason", sorted(FAILURE_REASONS))
def test_each_generic_failure_reason_is_shown_as_it_is(
    client: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    list_made: Made,
    admin_token: str,
    reason: str,
) -> None:
    seeded = accounts.seeded
    document = _live_document(
        admin,
        list_made,
        seeded,
        title=f"Reason {reason}",
        created=BASE_TIME,
        status="failed",
        failure_reason=reason,
    )

    items = {item["id"]: item for item in _listing(client, admin_token, limit=50).json()["items"]}

    assert items[str(document)]["latest_failure_reason"] == reason


def test_stored_text_that_is_not_a_generic_reason_is_never_returned(
    client: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    list_made: Made,
    admin_token: str,
) -> None:
    """The column holds free text. Anything that is not a generic reason is replaced by null, and never echoed."""
    seeded = accounts.seeded
    raw_text = "RAW-ERROR SECRET-7f3a9 from /private/path"
    document = _live_document(
        admin,
        list_made,
        seeded,
        title="Raw",
        created=BASE_TIME,
        status="failed",
        failure_reason=raw_text,
    )

    response = _listing(client, admin_token, limit=50)

    item = {i["id"]: i for i in response.json()["items"]}[str(document)]
    assert item["latest_failure_reason"] is None
    assert "SECRET-7f3a9" not in response.text

"""POST /documents/{id}/versions over HTTP (ADR 0024, migration 0013).

Same limits as POST /documents, because both use the same checks. Replacing a document that is missing, deleted or
another tenant's gives the same 404. Identical bytes to the current version give 409 with the existing ids.
Two uploads at once to one document take distinct version numbers: the document lock serializes them.
"""

import hashlib
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from grounded_answers.config import get_settings
from grounded_answers.main import create_app
from grounded_answers.passwords import hash_password

from .auth_support import SELECTOR, Accounts
from .ingest_support import Made, make_version
from .seed import Seeded

pytestmark = pytest.mark.db

MARKDOWN = "text/markdown"
TEXT_A = b"# 3 Probation\n\n3.2 Probation is six months for LTV drivers.\n"
TEXT_B = b"# 4 Review\n\n4.1 A review happens each year.\n"


@pytest.fixture
def upload_on(monkeypatch: MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("UPLOAD_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def replaced(
    admin: psycopg.Connection, made: Made, accounts: Accounts
) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """A live document whose version 1 is ready and current, with bytes TEXT_A (so the same bytes are a duplicate)."""
    seeded = accounts.seeded
    document, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=TEXT_A,
        status="ready",
    )
    admin.execute(
        "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s", (version, document)
    )
    admin.execute(
        "UPDATE grounded.document_versions SET sha256 = %s WHERE id = %s",
        (hashlib.sha256(TEXT_A).digest(), version),
    )
    yield document, version
    # The replacements the test made over HTTP are not in `made`, so they are removed here, with their bytes, audit rows
    # and queued jobs. The queue's delete trigger names its table without a schema, so the session needs queue on its path.
    admin.execute("SET search_path TO queue, grounded, public")
    admin.execute(
        "DELETE FROM queue.procrastinate_jobs WHERE args->>'document_id' = %s", (str(document),)
    )
    admin.execute(
        "DELETE FROM grounded.audit_log WHERE target_id IN "
        "(SELECT id FROM grounded.document_versions WHERE document_id = %s) OR target_id = %s",
        (document, document),
    )
    admin.execute(
        "DELETE FROM grounded.document_blobs WHERE version_id IN "
        "(SELECT id FROM grounded.document_versions WHERE document_id = %s)",
        (document,),
    )
    admin.execute(
        "UPDATE grounded.documents SET current_version_id = NULL WHERE id = %s", (document,)
    )
    admin.execute("DELETE FROM grounded.document_versions WHERE document_id = %s", (document,))


@pytest.fixture
def client_for(accounts: Accounts) -> Iterator[TestClient]:
    with TestClient(
        create_app(), raise_server_exceptions=False, backend_options=SELECTOR
    ) as test_client:
        yield test_client


def _token(client: TestClient, email: str, password: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


def _post(
    client: TestClient, token: str, document: uuid.UUID, data: bytes, name: str = "policy.md"
) -> Any:
    return client.post(
        f"/documents/{document}/versions",
        files={"file": (name, data, "text/markdown")},
        headers={"Authorization": f"Bearer {token}"},
    )


def _version_numbers(admin: psycopg.Connection, document: uuid.UUID) -> list[int]:
    rows = admin.execute(
        "SELECT version_no FROM grounded.document_versions WHERE document_id = %s ORDER BY version_no",
        (document,),
    ).fetchall()
    return [int(row[0]) for row in rows]


def test_a_replacement_is_queued_with_the_next_version_number_and_a_job(
    upload_on: None,
    client_for: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    replaced: tuple[uuid.UUID, uuid.UUID],
) -> None:
    document, _current = replaced
    token = _token(client_for, accounts.admin_email, accounts.password)

    response = _post(client_for, token, document, TEXT_B)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version_no"] == 2 and body["status"] == "queued"
    assert _version_numbers(admin, document) == [1, 2]
    jobs = admin.execute(
        "SELECT count(*) FROM queue.procrastinate_jobs WHERE task_name = 'grounded.ingest_document' AND status = 'todo' "
        "AND args->>'version_id' = %s",
        (body["version_id"],),
    ).fetchone()
    assert jobs == (1,), "the ingest job is enqueued in the same transaction as the version"


def test_identical_bytes_to_the_current_version_are_refused_with_its_ids(
    upload_on: None,
    client_for: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    replaced: tuple[uuid.UUID, uuid.UUID],
) -> None:
    document, current = replaced
    token = _token(client_for, accounts.admin_email, accounts.password)

    response = _post(client_for, token, document, TEXT_A)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "This file is already the current version",
        "document_id": str(document),
        "version_id": str(current),
    }
    assert _version_numbers(admin, document) == [1]


def test_a_deleted_document_is_a_404_like_an_unknown_one(
    upload_on: None,
    client_for: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    replaced: tuple[uuid.UUID, uuid.UUID],
) -> None:
    document, _current = replaced
    token = _token(client_for, accounts.admin_email, accounts.password)
    admin.execute(
        "UPDATE grounded.documents SET deleted_at = now(), purge_after = now() + interval '30 days' WHERE id = %s",
        (document,),
    )

    deleted = _post(client_for, token, document, TEXT_B)
    unknown = _post(client_for, token, uuid.uuid4(), TEXT_B)

    assert deleted.status_code == unknown.status_code == 404
    assert deleted.json() == unknown.json() == {"detail": "Not Found"}
    assert _version_numbers(admin, document) == [1]


def test_an_employee_is_refused(
    upload_on: None,
    client_for: TestClient,
    accounts: Accounts,
    replaced: tuple[uuid.UUID, uuid.UUID],
) -> None:
    document, _current = replaced
    token = _token(client_for, accounts.employee_email, accounts.password)

    response = _post(client_for, token, document, TEXT_B)

    assert response.status_code == 403


def test_another_tenants_admin_gets_the_same_404(
    upload_on: None,
    client_for: TestClient,
    accounts: Accounts,
    admin: psycopg.Connection,
    replaced: tuple[uuid.UUID, uuid.UUID],
) -> None:
    seeded: Seeded = accounts.seeded
    document, _current = replaced
    original = admin.execute(
        "SELECT password_hash FROM grounded.users WHERE id = %s", (seeded.b.admin,)
    ).fetchone()
    assert original is not None
    admin.execute(
        "UPDATE grounded.users SET password_hash = %s WHERE id = %s",
        (hash_password(accounts.password), seeded.b.admin),
    )
    try:
        b_email = admin.execute(
            "SELECT email FROM grounded.users WHERE id = %s", (seeded.b.admin,)
        ).fetchone()
        assert b_email is not None
        token = _token(client_for, b_email[0], accounts.password)
        response = _post(client_for, token, document, TEXT_B)
    finally:
        admin.execute(
            "UPDATE grounded.users SET password_hash = %s WHERE id = %s",
            (original[0], seeded.b.admin),
        )

    assert response.status_code == 404 and response.json() == {"detail": "Not Found"}
    assert _version_numbers(admin, document) == [1]


def test_two_replacements_at_once_take_distinct_version_numbers(
    upload_on: None,
    accounts: Accounts,
    admin: psycopg.Connection,
    replaced: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Both requests run at the same time. The document lock makes each read the maximum after the other's insert,
    so the numbers are 2 and 3, never two 2s. Each request has its own client, so each has its own app."""
    document, _current = replaced
    with TestClient(create_app(), backend_options=SELECTOR) as setup:
        token = _token(setup, accounts.admin_email, accounts.password)

    def upload(data: bytes) -> tuple[int, int | None]:
        with TestClient(
            create_app(), raise_server_exceptions=False, backend_options=SELECTOR
        ) as test_client:
            response = _post(test_client, token, document, data, name=f"{len(data)}.md")
            return response.status_code, response.json().get("version_no")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(upload, [TEXT_B, TEXT_B + b"\n\n5.1 Extra clause."])

    assert first[0] == 201 and second[0] == 201
    assert first[1] is not None and second[1] is not None
    assert sorted([first[1], second[1]]) == [2, 3]
    assert _version_numbers(admin, document) == [1, 2, 3]

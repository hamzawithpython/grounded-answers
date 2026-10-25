"""
POST /documents, end to end over HTTP against PostgreSQL and Valkey (SPEC §6.1, ADR 0019).

Each test logs in for real, sends real requests, and checks the rows that were written. The
rollback test forces a failure at the last step and checks that nothing was kept.
"""

import hashlib
import io
import uuid
import zipfile
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from redis.asyncio import Redis

from grounded_answers import documents_api
from grounded_answers.config import get_settings
from grounded_answers.upload.limits import MAX_FILE_BYTES
from grounded_answers.upload.rate import UploadLimiter

from .auth_support import Accounts
from .database import DbHandle

pytestmark = pytest.mark.db


@pytest.fixture
def upload_on(accounts: Accounts, monkeypatch: MonkeyPatch) -> Iterator[None]:
    """The feature switch on for one test (ADR 0019): the default is off."""
    monkeypatch.setenv("UPLOAD_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def made(accounts: Accounts) -> Iterator[list[str]]:
    """Document ids the test created. They, their versions, bytes, audit rows and jobs are removed."""
    documents: list[str] = []
    yield documents
    with psycopg.connect(accounts.handle.admin_test_url(), autocommit=True) as admin:
        # Procrastinate's delete trigger names its table without a schema, so this session needs
        # queue on its search path (the app and worker roles get it from the bootstrap).
        admin.execute("SET search_path TO queue, grounded, public")
        admin.execute(
            "DELETE FROM queue.procrastinate_jobs WHERE task_name = 'grounded.ingest_document'"
        )
        admin.execute("DELETE FROM grounded.audit_log WHERE action = 'document.upload'")
        for document in documents:
            admin.execute(
                "DELETE FROM grounded.document_blobs WHERE version_id IN "
                "(SELECT id FROM grounded.document_versions WHERE document_id = %s)",
                (document,),
            )
            admin.execute(
                "DELETE FROM grounded.document_versions WHERE document_id = %s", (document,)
            )
            admin.execute("DELETE FROM grounded.documents WHERE id = %s", (document,))


def _token(client: TestClient, email: str, password: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


def _admin_token(client: TestClient, accounts: Accounts) -> str:
    return _token(client, accounts.admin_email, accounts.password)


def _employee_token(client: TestClient, accounts: Accounts) -> str:
    return _token(client, accounts.employee_email, accounts.password)


def _post(
    client: TestClient,
    token: str | None,
    *,
    name: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    made: list[str] | None = None,
) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.post(
        "/documents", headers=headers, files={"file": (name, data, content_type)}
    )
    if made is not None and response.status_code in (201, 409):
        made.append(response.json()["document_id"])
    return response


def _count(handle: DbHandle, sql: str, params: tuple[Any, ...] = ()) -> int:
    with psycopg.connect(handle.admin_test_url()) as admin:
        row = admin.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


PDF = b"%PDF-1.7\n% a small test file\n"
TEXT = b"Leave policy: 25 days a year.\n"


def _docx(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


GOOD_DOCX = _docx({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"})


# ---- the happy path and the rules around it -----------------------------------------------------


def test_upload_is_off_by_default(
    client: TestClient, accounts: Accounts, monkeypatch: MonkeyPatch
) -> None:
    """With the switch set off, the endpoint answers 404. The variable is set explicitly: a developer's backend/.env may
    turn the switch on (the runbook's step 3a does), and the environment overrides that file. The default itself is tested
    without any file (tests/test_upload_switch_default.py)."""
    monkeypatch.setenv("UPLOAD_ENABLED", "false")
    get_settings.cache_clear()
    token = _admin_token(client, accounts)
    response = _post(client, token, name="policy.txt", data=TEXT)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_an_admin_upload_writes_the_document_version_bytes_audit_and_job(
    client: TestClient, accounts: Accounts, upload_on: None, made: list[str]
) -> None:
    token = _admin_token(client, accounts)
    response = _post(client, token, name="policy.pdf", data=PDF, made=made)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"
    document_id, version_id = body["document_id"], body["version_id"]
    tenant = str(accounts.seeded.a.tenant)

    handle = accounts.handle
    row = _admin_row(
        handle,
        "SELECT v.status, v.size_bytes, v.sha256, v.storage_key, v.content_type, "
        "v.original_filename FROM grounded.document_versions AS v WHERE v.id = %s",
        (version_id,),
    )
    assert row[0] == "queued"
    assert row[1] == len(PDF)
    assert bytes(row[2]) == hashlib.sha256(PDF).digest()
    assert row[3].startswith(f"tenant/{tenant}/")
    assert row[4] == "application/pdf"
    assert row[5] == "policy.pdf"
    assert (
        _count(
            handle,
            "SELECT count(*) FROM grounded.document_blobs WHERE version_id = %s",
            (version_id,),
        )
        == 1
    )
    audit = _admin_row(
        handle,
        "SELECT target_id, detail::text FROM grounded.audit_log WHERE action = 'document.upload' "
        "AND target_id = %s",
        (document_id,),
    )
    assert audit[0] == uuid.UUID(document_id)
    assert "policy.pdf" not in audit[1], "the audit row must not carry the filename"
    assert (
        _count(
            handle,
            "SELECT count(*) FROM queue.procrastinate_jobs WHERE task_name = 'grounded.ingest_document' "
            "AND args->>'version_id' = %s AND args->>'tenant_id' = %s",
            (version_id, tenant),
        )
        == 1
    )


def _admin_row(handle: DbHandle, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...]:
    with psycopg.connect(handle.admin_test_url()) as admin:
        row = admin.execute(sql, params).fetchone()
    assert row is not None
    return tuple(row)


def test_an_employee_is_refused(client: TestClient, accounts: Accounts, upload_on: None) -> None:
    token = _employee_token(client, accounts)
    response = _post(client, token, name="policy.txt", data=TEXT)
    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


def test_no_token_is_refused(client: TestClient, upload_on: None) -> None:
    response = _post(client, None, name="policy.txt", data=TEXT)
    assert response.status_code == 401


def test_the_client_content_type_header_is_ignored(
    client: TestClient, accounts: Accounts, upload_on: None, made: list[str]
) -> None:
    """A fake PDF labelled application/pdf is still refused: the bytes decide, not the label."""
    token = _admin_token(client, accounts)
    response = _post(
        client, token, name="policy.pdf", data=b"not a pdf", content_type="application/pdf"
    )
    assert response.status_code == 415
    assert response.json() == {"detail": "This file type is not supported"}


@pytest.mark.parametrize(
    ("name", "data", "status"),
    [
        ("fake.pdf", b"hello", 415),
        ("notadocx.docx", _docx({"readme.txt": b"hi"}), 415),
        ("bomb.docx", _docx({f"f{i}.txt": b"x" for i in range(2001)}), 415),
        ("binary.txt", b"caf\xe9", 415),
        ("nul.md", b"hello\x00world", 415),
        ("script.exe", b"MZ", 415),
        ("empty.txt", b"", 400),
    ],
    ids=["fake-pdf", "zip-not-docx", "zip-bomb", "not-utf8", "nul-byte", "extension", "empty"],
)
def test_refused_files_get_a_generic_answer_and_write_nothing(
    client: TestClient,
    accounts: Accounts,
    upload_on: None,
    name: str,
    data: bytes,
    status: int,
) -> None:
    token = _admin_token(client, accounts)
    before = _count(accounts.handle, "SELECT count(*) FROM grounded.document_versions")
    response = _post(client, token, name=name, data=data)
    assert response.status_code == status
    assert set(response.json()) == {"detail"}, "no internal detail in the body"
    assert _count(accounts.handle, "SELECT count(*) FROM grounded.document_versions") == before


def test_a_file_over_10_mib_is_refused_by_the_endpoint(
    client: TestClient, accounts: Accounts, upload_on: None
) -> None:
    """The body is just inside the cap (file + framing), so the endpoint's own check answers."""
    token = _admin_token(client, accounts)
    response = _post(client, token, name="big.txt", data=b"a" * (MAX_FILE_BYTES + 1))
    assert response.status_code == 413
    assert response.json() == {"detail": "The file is larger than 10 MiB"}


def test_a_chunked_body_over_the_cap_is_refused_without_a_content_length(
    client: TestClient, accounts: Accounts, upload_on: None
) -> None:
    """The stream is cut off at the cap; Content-Length is absent, so only the byte counter sees it."""
    token = _admin_token(client, accounts)
    boundary = "xxBOUNDARYxx"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()

    def chunks() -> Iterator[bytes]:
        yield head
        for _ in range(20):
            yield b"a" * (1024 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    response = client.post(
        "/documents",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        content=chunks(),
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "The request is too large"}


def test_many_form_fields_are_refused(
    client: TestClient, accounts: Accounts, upload_on: None
) -> None:
    token = _admin_token(client, accounts)
    fields = {f"field{i}": "x" for i in range(20)}
    response = client.post(
        "/documents",
        headers={"Authorization": f"Bearer {token}"},
        data=fields,
        files={"file": ("policy.txt", TEXT, "text/plain")},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "The form is not valid"}


def test_two_files_are_refused(client: TestClient, accounts: Accounts, upload_on: None) -> None:
    token = _admin_token(client, accounts)
    response = client.post(
        "/documents",
        headers={"Authorization": f"Bearer {token}"},
        files=[
            ("file", ("one.txt", TEXT, "text/plain")),
            ("file", ("two.txt", TEXT, "text/plain")),
        ],
    )
    assert response.status_code == 400


def test_a_missing_file_is_refused(client: TestClient, accounts: Accounts, upload_on: None) -> None:
    token = _admin_token(client, accounts)
    response = client.post(
        "/documents", headers={"Authorization": f"Bearer {token}"}, data={"note": "no file"}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "A file is required"}


def test_a_pdf_and_a_docx_are_accepted(
    client: TestClient, accounts: Accounts, upload_on: None, made: list[str]
) -> None:
    token = _admin_token(client, accounts)
    pdf = _post(client, token, name="a.pdf", data=PDF, made=made)
    docx = _post(
        client,
        token,
        name="b.docx",
        data=GOOD_DOCX,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        made=made,
    )
    assert pdf.status_code == 201
    assert docx.status_code == 201


def test_a_path_in_the_filename_is_stripped(
    client: TestClient, accounts: Accounts, upload_on: None, made: list[str]
) -> None:
    token = _admin_token(client, accounts)
    response = _post(client, token, name="../../etc/passwd-notes.txt", data=TEXT, made=made)
    assert response.status_code == 201
    stored = _admin_row(
        accounts.handle,
        "SELECT original_filename FROM grounded.document_versions WHERE id = %s",
        (response.json()["version_id"],),
    )[0]
    assert stored == "passwd-notes.txt"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("invoice‮gpj.txt", "invoicegpj.txt"),
        ("fi​le-notes.txt", "file-notes.txt"),
        ("Politique_sécurité.txt", "Politique_sécurité.txt"),
    ],
    ids=["rtl-override", "zero-width", "unicode-kept"],
)
def test_a_name_with_hidden_characters_is_stored_clean(
    client: TestClient,
    accounts: Accounts,
    upload_on: None,
    made: list[str],
    raw: str,
    expected: str,
) -> None:
    token = _admin_token(client, accounts)
    response = _post(client, token, name=raw, data=TEXT, made=made)
    assert response.status_code == 201
    stored = _admin_row(
        accounts.handle,
        "SELECT original_filename FROM grounded.document_versions WHERE id = %s",
        (response.json()["version_id"],),
    )[0]
    assert stored == expected
    assert "‮" not in stored and "​" not in stored


# ---- duplicates ---------------------------------------------------------------------------------


def test_the_same_file_twice_returns_409_with_the_existing_document(
    client: TestClient, accounts: Accounts, upload_on: None, made: list[str]
) -> None:
    token = _admin_token(client, accounts)
    first = _post(client, token, name="dup.txt", data=TEXT, made=made)
    second = _post(client, token, name="dup-again.txt", data=TEXT, made=made)
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json() == {
        "detail": "This file was already uploaded",
        "document_id": first.json()["document_id"],
    }


def test_a_deleted_document_does_not_block_its_file_from_being_uploaded_again(
    client: TestClient, accounts: Accounts, upload_on: None, made: list[str]
) -> None:
    """Only live documents count as duplicates (decision D5, owner's rule)."""
    token = _admin_token(client, accounts)
    first = _post(client, token, name="again.txt", data=TEXT, made=made)
    assert first.status_code == 201
    document_id = first.json()["document_id"]
    with psycopg.connect(accounts.handle.admin_test_url(), autocommit=True) as admin:
        admin.execute(
            "UPDATE grounded.documents SET deleted_at = now(), purge_after = now() + interval '30 days' "
            "WHERE id = %s",
            (document_id,),
        )
        admin.execute(
            "UPDATE grounded.document_versions SET status = 'retired', retired_at = now(), "
            "purge_after = now() + interval '30 days' WHERE document_id = %s",
            (document_id,),
        )
    second = _post(client, token, name="again.txt", data=TEXT, made=made)
    assert second.status_code == 201
    assert second.json()["document_id"] != document_id


# ---- isolation, rollback, limits ----------------------------------------------------------------


def test_another_tenant_cannot_see_the_upload(
    client: TestClient, accounts: Accounts, upload_on: None, made: list[str]
) -> None:
    """Row-level security: a session in tenant B finds no row of tenant A's new document."""
    token = _admin_token(client, accounts)
    response = _post(client, token, name="private.txt", data=TEXT, made=made)
    document_id = response.json()["document_id"]
    other = accounts.seeded.b.tenant
    with psycopg.connect(
        accounts.handle.url_for("app_user", accounts.handle.passwords["app_user"])
    ) as app:
        app.execute("SELECT set_config('app.current_tenant', %s, true)", (str(other),))
        row = app.execute(
            "SELECT count(*) FROM grounded.documents WHERE id = %s", (document_id,)
        ).fetchone()
    assert row == (0,)


def test_a_failure_at_the_last_step_keeps_nothing(
    client: TestClient,
    accounts: Accounts,
    upload_on: None,
    monkeypatch: MonkeyPatch,
) -> None:
    """The outbox guarantee: document, version, bytes, audit and job all roll back together."""
    marker = f"rollback-{uuid.uuid4().hex}.txt"

    async def fail(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(documents_api, "enqueue_job", fail)
    token = _admin_token(client, accounts)
    response = client.post(
        "/documents",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (marker, TEXT, "text/plain")},
    )
    assert response.status_code == 500
    assert marker.encode() not in response.content, "a 500 must not echo the filename"
    handle = accounts.handle
    assert (
        _count(
            handle,
            "SELECT count(*) FROM grounded.document_versions WHERE original_filename = %s",
            (marker,),
        )
        == 0
    )
    assert (
        _count(handle, "SELECT count(*) FROM grounded.documents WHERE title = %s", (marker,)) == 0
    )
    assert (
        _count(
            handle,
            "SELECT count(*) FROM grounded.audit_log WHERE action = 'document.upload' AND detail::text LIKE %s",
            (f"%{marker}%",),
        )
        == 0
    )


def test_the_rate_limit_per_tenant_returns_429_with_retry_after(
    client: TestClient, accounts: Accounts, upload_on: None, made: list[str]
) -> None:
    client.app.state.upload_limiter = UploadLimiter(  # type: ignore[attr-defined]
        client.app.state.upload_limiter.cache,  # type: ignore[attr-defined]
        b"x" * 48,
        lambda: 1_800_000_000_000,
        tenant_limit=2,
        user_limit=100,
    )
    token = _admin_token(client, accounts)
    statuses = [
        _post(
            client, token, name=f"rate{i}.txt", data=f"rate {i}\n".encode(), made=made
        ).status_code
        for i in range(3)
    ]
    assert statuses == [201, 201, 429]
    limited = _post(client, token, name="rate-last.txt", data=b"last\n")
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    assert limited.json() == {"detail": "Too many uploads. Try again later."}


def test_the_rate_limit_per_user_applies_to_one_user(
    client: TestClient, accounts: Accounts, upload_on: None, made: list[str]
) -> None:
    client.app.state.upload_limiter = UploadLimiter(  # type: ignore[attr-defined]
        client.app.state.upload_limiter.cache,  # type: ignore[attr-defined]
        b"y" * 48,
        lambda: 1_800_000_000_000,
        tenant_limit=100,
        user_limit=1,
    )
    token = _admin_token(client, accounts)
    assert _post(client, token, name="user1.txt", data=b"u1\n", made=made).status_code == 201
    assert _post(client, token, name="user2.txt", data=b"u2\n").status_code == 429


def test_valkey_unavailable_fails_closed_with_503(
    client: TestClient, accounts: Accounts, upload_on: None
) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=1, socket_timeout=1)
    client.app.state.upload_limiter = UploadLimiter(  # type: ignore[attr-defined]
        dead, b"z" * 48, lambda: 1_800_000_000_000
    )
    token = _admin_token(client, accounts)
    response = _post(client, token, name="closed.txt", data=TEXT)
    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}

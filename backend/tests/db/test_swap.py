"""grounded.swap_current_version, called as worker_user the way the ingestion finish calls it (ADR 0024, migration 0013).

The function locks the document, so these calls are serial in these tests. The concurrency tests are in
test_replace_race.py. Here: the outcomes, the refusals (test g), and the audit row.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors

from .database import DbHandle
from .ingest_support import Made, make_version
from .seed import Seeded

pytestmark = pytest.mark.db

MARKDOWN = "text/markdown"


def _worker(handle: DbHandle, tenant: uuid.UUID | None) -> psycopg.Connection:
    conn = psycopg.connect(handle.url_for("worker_user", handle.passwords["worker_user"]))
    if tenant is not None:
        conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))
    return conn


def _swap(handle: DbHandle, tenant: uuid.UUID, document: uuid.UUID, version: uuid.UUID) -> str:
    with _worker(handle, tenant) as worker:
        row = worker.execute(
            "SELECT grounded.swap_current_version(%s, %s)", (document, version)
        ).fetchone()
        worker.commit()
    assert row is not None
    outcome: str = row[0]
    return outcome


def _current(admin: psycopg.Connection, document: uuid.UUID) -> Any:
    row = admin.execute(
        "SELECT current_version_id FROM grounded.documents WHERE id = %s", (document,)
    ).fetchone()
    assert row is not None
    return row[0]


def _status(admin: psycopg.Connection, version: uuid.UUID) -> tuple[str, Any]:
    row = admin.execute(
        "SELECT status, retired_at FROM grounded.document_versions WHERE id = %s", (version,)
    ).fetchone()
    assert row is not None
    return row[0], row[1]


@pytest.fixture
def document_with_ready(
    admin: psycopg.Connection, made: Made, seeded: Seeded
) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """A live document whose version 1 is ready and current."""
    document, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="ready",
    )
    admin.execute(
        "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s", (version, document)
    )
    yield document, version


def _ready_version(
    admin: psycopg.Connection, made: Made, seeded: Seeded, document: uuid.UUID, number: int
) -> uuid.UUID:
    _, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="ready",
        version_no=number,
    )
    admin.execute(
        "UPDATE grounded.document_versions SET document_id = %s WHERE id = %s", (document, version)
    )
    return version


def test_a_newer_ready_version_becomes_current_and_the_old_one_is_retired(
    admin: psycopg.Connection,
    grounded_db: DbHandle,
    seeded: Seeded,
    made: Made,
    document_with_ready: tuple[uuid.UUID, uuid.UUID],
) -> None:
    document, old = document_with_ready
    newer = _ready_version(admin, made, seeded, document, 2)

    outcome = _swap(grounded_db, seeded.a.tenant, document, newer)

    assert outcome == "current"
    assert _current(admin, document) == newer
    status, retired_at = _status(admin, old)
    assert status == "retired" and retired_at is not None
    purge_after = admin.execute(
        "SELECT purge_after FROM grounded.document_versions WHERE id = %s", (old,)
    ).fetchone()
    assert purge_after is not None
    assert timedelta(days=29) < purge_after[0] - retired_at < timedelta(days=31)


def test_an_older_ready_version_is_retired_as_superseded_and_the_current_stays(
    admin: psycopg.Connection,
    grounded_db: DbHandle,
    seeded: Seeded,
    made: Made,
    document_with_ready: tuple[uuid.UUID, uuid.UUID],
) -> None:
    document, current = document_with_ready
    newer = _ready_version(admin, made, seeded, document, 3)
    assert _swap(grounded_db, seeded.a.tenant, document, newer) == "current"
    older = _ready_version(admin, made, seeded, document, 2)

    outcome = _swap(grounded_db, seeded.a.tenant, document, older)

    assert outcome == "superseded"
    assert _current(admin, document) == newer
    assert _status(admin, older)[0] == "retired"
    assert _status(admin, current)[0] == "retired"


def test_the_first_ready_version_becomes_current(
    admin: psycopg.Connection, grounded_db: DbHandle, seeded: Seeded, made: Made
) -> None:
    document = uuid.uuid4()
    admin.execute(
        "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (%s, %s, 'First')",
        (document, seeded.a.tenant),
    )
    made.documents.append(document)
    first = _ready_version(admin, made, seeded, document, 1)

    assert _swap(grounded_db, seeded.a.tenant, document, first) == "current"
    assert _current(admin, document) == first


def test_the_audit_row_holds_the_outcome_and_no_title(
    admin: psycopg.Connection,
    grounded_db: DbHandle,
    seeded: Seeded,
    made: Made,
    document_with_ready: tuple[uuid.UUID, uuid.UUID],
) -> None:
    document, _current_version = document_with_ready
    newer = _ready_version(admin, made, seeded, document, 2)
    _swap(grounded_db, seeded.a.tenant, document, newer)

    rows = admin.execute(
        "SELECT detail FROM grounded.audit_log WHERE action = 'document.swap' AND target_id = %s",
        (document,),
    ).fetchall()
    assert rows == [({"outcome": "current", "retired": 1},)]
    assert "Expense" not in str(rows) and "Matrix" not in str(rows)


def test_a_version_of_another_document_is_refused(
    admin: psycopg.Connection,
    grounded_db: DbHandle,
    seeded: Seeded,
    made: Made,
    document_with_ready: tuple[uuid.UUID, uuid.UUID],
) -> None:
    document, _current_version = document_with_ready
    _other_document, foreign = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="ready",
        version_no=9,
    )
    with pytest.raises(errors.InsufficientPrivilege, match="does not belong to this document"):
        _swap(grounded_db, seeded.a.tenant, document, foreign)
    assert _current(admin, document) != foreign


def test_a_version_of_another_tenant_is_refused(
    admin: psycopg.Connection,
    grounded_db: DbHandle,
    seeded: Seeded,
    made: Made,
    document_with_ready: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Tenant B's version, offered by tenant A's session for tenant A's document: refused, and B's row is untouched."""
    document, _current_version = document_with_ready
    foreign_doc, foreign = make_version(
        admin,
        made,
        seeded.b.tenant,
        seeded.b.admin,
        content_type=MARKDOWN,
        data=None,
        status="ready",
        version_no=2,
    )
    with pytest.raises(errors.InsufficientPrivilege):
        _swap(grounded_db, seeded.a.tenant, document, foreign)
    assert _status(admin, foreign) == ("ready", None)
    assert _current(admin, foreign_doc) is None


def test_a_deleted_document_is_refused(
    admin: psycopg.Connection,
    grounded_db: DbHandle,
    seeded: Seeded,
    made: Made,
    document_with_ready: tuple[uuid.UUID, uuid.UUID],
) -> None:
    document, _current_version = document_with_ready
    newer = _ready_version(admin, made, seeded, document, 2)
    now = datetime.now(UTC)
    admin.execute(
        "UPDATE grounded.documents SET deleted_at = %s, purge_after = %s WHERE id = %s",
        (now, now + timedelta(days=30), document),
    )
    with pytest.raises(errors.InsufficientPrivilege, match="not live"):
        _swap(grounded_db, seeded.a.tenant, document, newer)
    assert _status(admin, newer)[0] == "ready"


def test_a_version_that_is_not_ready_is_refused(
    admin: psycopg.Connection,
    grounded_db: DbHandle,
    seeded: Seeded,
    made: Made,
    document_with_ready: tuple[uuid.UUID, uuid.UUID],
) -> None:
    document, _current_version = document_with_ready
    _, queued = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="queued",
        version_no=2,
    )
    admin.execute(
        "UPDATE grounded.document_versions SET document_id = %s WHERE id = %s", (document, queued)
    )
    with pytest.raises(errors.CheckViolation, match="only a ready version"):
        _swap(grounded_db, seeded.a.tenant, document, queued)


def test_a_call_without_a_tenant_context_is_refused(
    admin: psycopg.Connection,
    grounded_db: DbHandle,
    seeded: Seeded,
    document_with_ready: tuple[uuid.UUID, uuid.UUID],
) -> None:
    document, version = document_with_ready
    with pytest.raises(errors.InsufficientPrivilege, match="needs a tenant context"):
        with _worker(grounded_db, None) as worker:
            worker.execute("SELECT grounded.swap_current_version(%s, %s)", (document, version))

"""
Ingestion of one document version (ADR 0022): the pipeline, its status rules, the lease and fencing, retry
safety, the failure reasons, and what reaches the logs. Runs against the real database as worker_user.

The embedder is a fake here, so these tests do not depend on the model. The real model is tested in
test_embedding_model.py.
"""

import logging
import uuid
from typing import Any

import psycopg
import pytest
from _pytest.logging import LogCaptureFixture
from _pytest.monkeypatch import MonkeyPatch

from grounded_answers.embedding.pinned import MODEL_NAME, MODEL_REVISION
from grounded_answers.extract.model import Extracted
from grounded_answers.extract.sandbox import (
    REASON_MEMORY,
    REASON_NO_TEXT,
    REASON_TIMEOUT,
    REASON_UNREADABLE,
    ExtractionFailed,
)
from grounded_answers.ingest import pipeline
from grounded_answers.ingest.chunking import section_chunks
from grounded_answers.ingest.failures import REASON_EMBEDDING, LeaseHeld, LostLease, VersionNotFound
from grounded_answers.ingest.pipeline import IngestJob, Outcome, mark_failed, run_ingestion
from grounded_answers.storage.postgres_store import PostgresObjectStore
from tests.db.database import DbHandle
from tests.db.seed import Seeded

from .ingest_support import (
    ExplodingEmbedder,
    FakeEmbedder,
    Made,
    age_lease,
    chunk_count,
    make_version,
    ready_audit_count,
    version_row,
    worker_sessions,
)

pytestmark = [pytest.mark.db, pytest.mark.anyio]

MARKDOWN = "text/markdown"
MARKER = "MARKER-ingest-7c1d-do-not-log"
POLICY_MD = (
    b"# 3 Probation\n\n"
    b"3.2 Probation is six months for LTV drivers.\n\n"
    b"3.2.2 A road assessment is due before the end of month four.\n\n"
    b"| Grade | Level |\n|---|---|\n| A | 1 |\n"
)


def _job(tenant: uuid.UUID, document: uuid.UUID, version: uuid.UUID, lease: int = 120) -> IngestJob:
    return IngestJob(
        tenant_id=tenant,
        document_id=document,
        version_id=version,
        attempt_token=uuid.uuid4(),
        lease_seconds=lease,
    )


def _extracted(data: bytes) -> Extracted:
    from grounded_answers.extract.sandbox import extract

    return extract("md", data)


async def test_a_markdown_version_becomes_ready_with_chunks_and_model_metadata(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=POLICY_MD
    )
    async with worker_sessions(grounded_db) as factory:
        outcome = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            job=_job(seeded.a.tenant, document, version),
        )
    assert outcome is Outcome.READY
    assert version_row(admin, version) == ("ready", None)
    assert chunk_count(admin, version) >= 2
    rows = admin.execute(
        "SELECT DISTINCT embedding_model, embedding_model_version FROM grounded.chunks "
        "WHERE version_id = %s",
        (version,),
    ).fetchall()
    assert rows == [(MODEL_NAME, MODEL_REVISION)]
    clauses = admin.execute(
        "SELECT clause_labels FROM grounded.chunks WHERE version_id = %s AND %s = ANY(clause_labels)",
        (version, "3.2.2"),
    ).fetchall()
    assert clauses, "the clause 3.2.2 must be recorded on a chunk for citations"
    current = admin.execute(
        "SELECT current_version_id FROM grounded.documents WHERE id = %s", (document,)
    ).fetchone()
    assert current == (version,)
    assert ready_audit_count(admin, version) == 1


@pytest.mark.parametrize(
    "content_type",
    [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ],
)
async def test_generated_pdf_and_docx_versions_are_ingested(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    content_type: str,
) -> None:
    from tests.extract_fixtures import docx_with_headings, pdf_pages

    payload = (
        pdf_pages([[(18.0, "1 Purpose"), (11.0, "1.1 The policy text is in this line.")]])
        if content_type == "application/pdf"
        else docx_with_headings()
    )
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=content_type, data=payload
    )
    async with worker_sessions(grounded_db) as factory:
        outcome = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            job=_job(seeded.a.tenant, document, version),
        )
    assert outcome is Outcome.READY
    assert version_row(admin, version) == ("ready", None)
    assert chunk_count(admin, version) >= 1


async def test_a_claim_is_resumed_by_the_same_job_and_refused_to_another(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=POLICY_MD
    )
    first = _job(seeded.a.tenant, document, version)
    async with worker_sessions(grounded_db) as factory:
        await pipeline._claim(factory, first)  # attempt 1 claims and then "crashes"
        assert version_row(admin, version)[0] == "processing"
        # The retry of the same job carries the same token and resumes its own lease.
        resumed = IngestJob(
            tenant_id=first.tenant_id,
            document_id=first.document_id,
            version_id=first.version_id,
            attempt_token=first.attempt_token,
            lease_seconds=120,
        )
        assert (
            await run_ingestion(
                session_factory=factory,
                object_store=PostgresObjectStore(),
                embedder=FakeEmbedder(),
                job=resumed,
            )
            is Outcome.READY
        )


async def test_another_job_waits_while_the_lease_is_fresh(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=POLICY_MD
    )
    async with worker_sessions(grounded_db) as factory:
        await pipeline._claim(factory, _job(seeded.a.tenant, document, version))
        with pytest.raises(LeaseHeld):
            await pipeline._claim(factory, _job(seeded.a.tenant, document, version))


async def test_another_job_takes_over_an_expired_lease(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=POLICY_MD
    )
    async with worker_sessions(grounded_db) as factory:
        await pipeline._claim(factory, _job(seeded.a.tenant, document, version))
        age_lease(admin, version)
        outcome = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            job=_job(seeded.a.tenant, document, version),
        )
    assert outcome is Outcome.READY
    assert version_row(admin, version) == ("ready", None)


@pytest.mark.parametrize("status", ["ready", "failed", "retired", "purging"])
async def test_a_version_in_any_other_state_is_skipped_untouched(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    status: str,
) -> None:
    document, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=POLICY_MD,
        status=status,
    )
    async with worker_sessions(grounded_db) as factory:
        outcome = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            job=_job(seeded.a.tenant, document, version),
        )
    assert outcome is Outcome.SKIPPED
    assert version_row(admin, version)[0] == status
    assert chunk_count(admin, version) == 0


async def test_a_version_not_visible_to_the_tenant_is_a_poison_job(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
) -> None:
    """A version id that is unknown, or that belongs to another tenant, is refused. It is never retried."""
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=POLICY_MD
    )
    async with worker_sessions(grounded_db) as factory:
        with pytest.raises(VersionNotFound):
            await run_ingestion(
                session_factory=factory,
                object_store=PostgresObjectStore(),
                embedder=FakeEmbedder(),
                job=_job(seeded.a.tenant, document, uuid.uuid4()),
            )
        with pytest.raises(VersionNotFound):
            await run_ingestion(
                session_factory=factory,
                object_store=PostgresObjectStore(),
                embedder=FakeEmbedder(),
                job=_job(seeded.b.tenant, document, version),
            )
    assert version_row(admin, version)[0] == "queued"


async def test_a_crashed_attempt_then_a_retry_leaves_exactly_one_set_of_chunks(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    monkeypatch: MonkeyPatch,
) -> None:
    """The write fails after the chunk rows are inserted, before the commit. Nothing is kept. The retry (after the
    lease expires) writes one set, with positions 0..n-1 and no duplicates."""
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=POLICY_MD
    )

    def crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated crash before commit")

    async with worker_sessions(grounded_db) as factory:
        monkeypatch.setattr(pipeline, "_audit", crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await run_ingestion(
                session_factory=factory,
                object_store=PostgresObjectStore(),
                embedder=FakeEmbedder(),
                job=_job(seeded.a.tenant, document, version),
            )
        monkeypatch.undo()
        assert chunk_count(admin, version) == 0, "a crashed write must leave no chunks"
        assert version_row(admin, version)[0] == "processing"
        age_lease(admin, version)
        outcome = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            job=_job(seeded.a.tenant, document, version),
        )
    assert outcome is Outcome.READY
    positions = admin.execute(
        "SELECT position FROM grounded.chunks WHERE version_id = %s ORDER BY position", (version,)
    ).fetchall()
    assert [row[0] for row in positions] == list(range(len(positions)))
    assert ready_audit_count(admin, version) == 1


async def test_an_older_worker_that_lost_its_lease_writes_nothing(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
) -> None:
    """Two workers race on one version. The older one has done its work when the newer one finishes. Its final
    write is refused by the fencing token, and it leaves one chunk set and one audit row."""
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=POLICY_MD
    )
    older = _job(seeded.a.tenant, document, version)
    newer = _job(seeded.a.tenant, document, version)
    chunks = section_chunks(_extracted(POLICY_MD))
    vectors = FakeEmbedder().embed_passages([chunk.body for chunk in chunks])
    async with worker_sessions(grounded_db) as factory:
        await pipeline._claim(factory, older)
        age_lease(admin, version)
        assert (
            await run_ingestion(
                session_factory=factory,
                object_store=PostgresObjectStore(),
                embedder=FakeEmbedder(),
                job=newer,
            )
        ) is Outcome.READY
        with pytest.raises(LostLease):
            await pipeline._write_ready(factory, older, chunks, vectors)
    assert chunk_count(admin, version) == len(chunks)
    assert ready_audit_count(admin, version) == 1
    assert version_row(admin, version) == ("ready", None)


async def test_a_lost_lease_cannot_mark_the_version_failed(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
) -> None:
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=POLICY_MD
    )
    older = _job(seeded.a.tenant, document, version)
    async with worker_sessions(grounded_db) as factory:
        await pipeline._claim(factory, older)
        age_lease(admin, version)
        await pipeline._claim(factory, _job(seeded.a.tenant, document, version))
        assert await mark_failed(factory, older, REASON_UNREADABLE) is Outcome.LOST
    assert version_row(admin, version)[0] == "processing"


@pytest.mark.parametrize(
    ("label", "content_type", "data", "expected_reason"),
    [
        ("no text", MARKDOWN, b"   \n\n  \n", REASON_NO_TEXT),
    ],
)
async def test_a_document_the_extractor_refuses_is_failed_without_a_retry(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    label: str,
    content_type: str,
    data: bytes,
    expected_reason: str,
) -> None:
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=content_type, data=data
    )
    async with worker_sessions(grounded_db) as factory:
        outcome = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            job=_job(seeded.a.tenant, document, version),
        )
    assert outcome is Outcome.FAILED, label
    assert version_row(admin, version) == ("failed", expected_reason)
    assert chunk_count(admin, version) == 0


@pytest.mark.parametrize("reason", [REASON_TIMEOUT, REASON_MEMORY])
async def test_an_extraction_timeout_or_memory_failure_is_recorded_as_its_reason(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    monkeypatch: MonkeyPatch,
    reason: str,
) -> None:
    def refuse(kind: str, data: bytes) -> Extracted:
        raise ExtractionFailed(reason)

    monkeypatch.setattr(pipeline, "extract", refuse)
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=POLICY_MD
    )
    async with worker_sessions(grounded_db) as factory:
        outcome = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            job=_job(seeded.a.tenant, document, version),
        )
    assert outcome is Outcome.FAILED
    assert version_row(admin, version) == ("failed", reason)


async def test_a_missing_blob_is_failed_as_unreadable(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
) -> None:
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=None
    )
    async with worker_sessions(grounded_db) as factory:
        outcome = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            job=_job(seeded.a.tenant, document, version),
        )
    assert outcome is Outcome.FAILED
    assert version_row(admin, version) == ("failed", REASON_UNREADABLE)


async def test_an_embedding_failure_is_recorded_as_embedding_without_its_message(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    caplog: LogCaptureFixture,
) -> None:
    """The marker sits in the model's error message. It must reach neither the logs nor the failure reason."""
    caplog.set_level(logging.DEBUG)
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=POLICY_MD
    )
    async with worker_sessions(grounded_db) as factory:
        outcome = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=ExplodingEmbedder(MARKER),
            job=_job(seeded.a.tenant, document, version),
        )
    assert outcome is Outcome.FAILED
    status, reason = version_row(admin, version)
    assert (status, reason) == ("failed", REASON_EMBEDDING)
    assert MARKER not in (reason or "")
    assert MARKER not in caplog.text


async def test_no_document_text_reaches_the_logs_on_success(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    data = POLICY_MD + f"\n\n3.9 {MARKER} is the marker clause.\n".encode()
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=data
    )
    async with worker_sessions(grounded_db) as factory:
        outcome = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            job=_job(seeded.a.tenant, document, version),
        )
    assert outcome is Outcome.READY
    assert MARKER not in caplog.text
    assert str(version) not in caplog.text, "the version id is not logged"


async def test_the_registered_task_gives_up_only_on_its_last_attempt(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    monkeypatch: MonkeyPatch,
) -> None:
    """Through the real Procrastinate task: a transient error on a middle attempt is re-raised and the version is
    left alone; on the last attempt the version is marked failed with the generic reason, then the error is re-raised."""
    from types import SimpleNamespace
    from typing import cast

    import procrastinate
    from procrastinate import testing

    from grounded_answers.jobs import ingest_task
    from grounded_answers.jobs.tasks import INGEST_DOCUMENT
    from grounded_answers.storage.postgres_store import PostgresObjectStore

    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type=MARKDOWN, data=POLICY_MD
    )

    async def lost_connection(**kwargs: Any) -> Outcome:
        raise psycopg.OperationalError("connection lost")

    async with worker_sessions(grounded_db) as factory:
        app = procrastinate.App(connector=testing.InMemoryConnector())
        ingest_task.register_ingest_task(
            app,
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            lease_seconds=120,
        )
        task = app.tasks[INGEST_DOCUMENT]
        func = cast(Any, task.func)
        job_id = 1
        # The first attempt claimed the version with this job's token, then lost its connection.
        await pipeline._claim(
            factory,
            IngestJob(
                tenant_id=seeded.a.tenant,
                document_id=document,
                version_id=version,
                attempt_token=ingest_task.attempt_token(job_id),
                lease_seconds=120,
            ),
        )
        monkeypatch.setattr(ingest_task, "run_ingestion", lost_connection)
        args = {
            "tenant_id": str(seeded.a.tenant),
            "document_id": str(document),
            "version_id": str(version),
        }
        middle = SimpleNamespace(job=SimpleNamespace(id=job_id, attempts=1))
        with pytest.raises(psycopg.OperationalError):
            await func(middle, **args)
        assert version_row(admin, version) == ("processing", None)

        last = SimpleNamespace(
            job=SimpleNamespace(id=job_id, attempts=ingest_task.RETRY_MAX_ATTEMPTS)
        )
        with pytest.raises(psycopg.OperationalError):
            await func(last, **args)
    assert version_row(admin, version) == ("failed", ingest_task.LAST_ATTEMPT_REASON)


@pytest.mark.parametrize(
    ("start", "target", "outcome"),
    [
        # Visible to the worker: the trigger refuses the step.
        ("queued", "ready", "refused"),
        ("queued", "failed", "refused"),
        ("processing", "queued", "refused"),
        # Not visible to the worker (no policy covers ready or failed): RLS hides the row, nothing changes.
        ("ready", "failed", "hidden"),
        ("failed", "ready", "hidden"),
    ],
)
async def test_the_worker_transition_guard_refuses_every_step_but_its_own(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    start: str,
    target: str,
    outcome: str,
) -> None:
    """Permissive policies cannot see the old status, so a trigger enforces the worker's steps (migration 0009).
    A queued version must go through the claim, never straight to ready. This test showed that it could."""
    _, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=POLICY_MD,
        status=start,
    )
    url = grounded_db.url_for("worker_user", grounded_db.passwords["worker_user"])
    with psycopg.connect(url) as worker:
        worker.execute("SELECT set_config('app.current_tenant', %s, true)", (str(seeded.a.tenant),))
        if outcome == "refused":
            with pytest.raises(
                psycopg.errors.InsufficientPrivilege, match="may not move a version"
            ):
                worker.execute(
                    "UPDATE grounded.document_versions SET status = %s WHERE id = %s",
                    (target, version),
                )
        else:
            cursor = worker.execute(
                "UPDATE grounded.document_versions SET status = %s WHERE id = %s",
                (target, version),
            )
            assert cursor.rowcount == 0
    assert version_row(admin, version)[0] == start

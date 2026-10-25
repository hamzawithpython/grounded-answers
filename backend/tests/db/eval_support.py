"""
Setup for the retrieval evaluation (Session 5, Step 1): a fresh tenant, and documents loaded through the real
ingestion pipeline (extract, chunk, embed, write, swap), as worker_user.

A document's identity (its ID and version) is read from its header text, never from the filename (docs/EVAL.md).
Versions of one document are loaded oldest first, so loading v2.0 runs the real swap and retires v1.0.
"""

import re
import secrets
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from grounded_answers.database import create_database_engine, create_session_factory
from grounded_answers.embedding.model import Embedder
from grounded_answers.evaluation.harness import LoadedVersion
from grounded_answers.extract.model import Extracted
from grounded_answers.extract.sandbox import extract
from grounded_answers.ingest.pipeline import IngestJob, Outcome, run_ingestion
from grounded_answers.storage.postgres_store import PostgresObjectStore
from grounded_answers.upload.detect import CONTENT_TYPE, Kind

from .database import DbHandle
from .ingest_support import Made, chunk_count, make_version, worker_sessions

KIND_BY_SUFFIX = {".md": Kind.MD, ".txt": Kind.TXT, ".docx": Kind.DOCX, ".pdf": Kind.PDF}
_DOCUMENT_ID = re.compile(r"^\s*Document ID\s*\|?\s*(IFL-[A-Z]+-\d+)\s*$", re.MULTILINE)
_VERSION = re.compile(r"^\s*Version\s*\|?\s*(\d+\.\d+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class EvalTenant:
    tenant: uuid.UUID
    admin: uuid.UUID
    employee: uuid.UUID


@dataclass(frozen=True)
class DemoFile:
    path: Path
    kind: Kind
    document: str
    version: str


def fresh_tenant(admin: psycopg.Connection) -> EvalTenant:
    """A tenant with an admin and an employee, so no chunk from another test can appear in the results.

    The rows are test setup, written as the cluster admin. The test database is rebuilt on the next run."""
    tenant, admin_user, employee = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    domain = f"eval-{secrets.token_hex(4)}.test"
    admin.execute(
        "INSERT INTO grounded.tenants (id, name, slug) VALUES (%s, 'Eval', %s)",
        (tenant, f"eval-{tenant.hex[:8]}"),
    )
    admin.execute(
        "INSERT INTO grounded.tenant_email_domains (id, tenant_id, domain) VALUES (%s, %s, %s)",
        (uuid.uuid4(), tenant, domain),
    )
    for user, role in ((admin_user, "tenant_admin"), (employee, "employee")):
        admin.execute(
            "INSERT INTO grounded.users (id, email, display_name) VALUES (%s, %s, %s)",
            (user, f"{role}.{user.hex[:8]}@{domain}", role),
        )
        admin.execute(
            "INSERT INTO grounded.tenant_memberships (id, tenant_id, user_id, role) "
            "VALUES (%s, %s, %s, %s)",
            (uuid.uuid4(), tenant, user, role),
        )
    return EvalTenant(tenant, admin_user, employee)


def header_identity(extracted: Extracted) -> tuple[str, str]:
    """(document ID, version) from the header table or header lines. Raises if either is missing."""
    lines: list[str] = []
    for section in extracted.sections:
        for table in section.tables:
            lines.extend(" | ".join(row) for row in table.rows)
        lines.append(section.text)
    header = "\n".join(lines)
    document, version = _DOCUMENT_ID.search(header), _VERSION.search(header)
    if document is None or version is None:
        raise ValueError("the document header has no Document ID or Version")
    return document.group(1), version.group(1)


def read_demo_files(directory: Path) -> list[DemoFile]:
    files = []
    for path in sorted(directory.iterdir()):
        kind = KIND_BY_SUFFIX[path.suffix]
        document, version = header_identity(extract(kind.value, path.read_bytes()))
        files.append(DemoFile(path, kind, document, version))
    return sorted(files, key=lambda f: (f.document, tuple(int(n) for n in f.version.split("."))))


async def load_files(
    handle: DbHandle,
    admin: psycopg.Connection,
    made: Made,
    tenant: EvalTenant,
    files: Sequence[DemoFile],
    embedder: Embedder,
) -> list[LoadedVersion]:
    """Ingest every file as worker_user. Each document's later version replaces the earlier one (the swap)."""
    documents: dict[str, uuid.UUID] = {}
    numbers: dict[str, int] = {}
    loaded: list[tuple[DemoFile, uuid.UUID]] = []
    async with worker_sessions(handle) as factory:
        for file in files:
            number = numbers.get(file.document, 0) + 1
            document, version = make_version(
                admin,
                made,
                tenant.tenant,
                tenant.admin,
                content_type=CONTENT_TYPE[file.kind],
                data=file.path.read_bytes(),
                # The upload endpoint uses the filename as the title (documents_api.py).
                title=file.path.name,
                version_no=number,
                document=documents.get(file.document),
            )
            documents[file.document] = document
            numbers[file.document] = number
            outcome = await run_ingestion(
                session_factory=factory,
                object_store=PostgresObjectStore(),
                embedder=embedder,
                job=IngestJob(
                    tenant_id=tenant.tenant,
                    document_id=document,
                    version_id=version,
                    attempt_token=uuid.uuid4(),
                    lease_seconds=120,
                    retention_days=30,
                ),
            )
            if outcome is not Outcome.READY:
                raise RuntimeError(f"{file.path.name} was not ingested: {outcome}")
            loaded.append((file, version))
    result = []
    for file, version in loaded:
        row = admin.execute(
            "SELECT d.current_version_id = %s FROM grounded.documents AS d WHERE d.id = %s",
            (version, documents[file.document]),
        ).fetchone()
        assert row is not None
        result.append(
            LoadedVersion(
                version, file.document, file.version, bool(row[0]), chunk_count(admin, version)
            )
        )
    return result


@asynccontextmanager
async def app_sessions(handle: DbHandle) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory for app_user, with its own engine. The engine is disposed at the end."""
    url = handle.url_for("app_user", handle.passwords["app_user"]).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    engine = create_database_engine(url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@asynccontextmanager
async def admin_sessions(handle: DbHandle) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory for the cluster admin, which bypasses row-level security. Only for the negative control."""
    url = handle.admin_test_url().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_database_engine(url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()

"""
The tenant session dependency commits before the response is sent (ADR 0005, decision D1).

The route inserts a document. The second insert points current_version_id at a version that does
not exist. documents_current_version_fk is DEFERRABLE INITIALLY DEFERRED, so PostgreSQL refuses
it only at COMMIT. That is after the route has returned. With scope="function" the commit runs
before the response, so the client gets a 500 and no row is stored. With the default scope the
client would get a 200 and the row would be lost.
"""

import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import psycopg
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from grounded_answers.database import create_database_engine, create_session_factory
from grounded_answers.runtime import selector_loop_factory
from grounded_answers.tenant_db import TenantContext, make_tenant_session
from tests.db.database import DbHandle
from tests.db.seed import Seeded
from tests.db.test_orm_models import app_url

pytestmark = pytest.mark.db

# TestClient runs the app on its own thread. Give it the selector loop psycopg needs on Windows
# (ADR 0005), the same loop uvicorn gets from --loop in production.
SELECTOR_LOOP: dict[str, Any] = {"loop_factory": selector_loop_factory}


def _build_app(url: str, tenant: uuid.UUID, user: uuid.UUID) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Created inside the app's own event loop, the same way the production lifespan does it.
        engine = create_database_engine(url)
        app.state.session_factory = create_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    async def get_context() -> TenantContext:
        return TenantContext(tenant_id=tenant, user_id=user)

    tenant_session = Annotated[
        AsyncSession,
        Depends(make_tenant_session(get_context), scope="function"),
    ]
    app = FastAPI(lifespan=lifespan)

    @app.post("/documents")
    async def create_document(
        session: tenant_session, title: str, dangling: bool = False
    ) -> dict[str, str]:
        document_id = uuid.uuid4()
        current_version = uuid.uuid4() if dangling else None
        await session.execute(
            text(
                "INSERT INTO grounded.documents (id, tenant_id, title, current_version_id) "
                "VALUES (:id, :tenant_id, :title, :current_version)"
            ),
            {
                "id": document_id,
                "tenant_id": tenant,
                "title": title,
                "current_version": current_version,
            },
        )
        return {"id": str(document_id)}

    return app


def _stored_titles(handle: DbHandle, title: str) -> int:
    with psycopg.connect(handle.admin_test_url()) as conn:
        row = conn.execute(
            "SELECT count(*) FROM grounded.documents WHERE title = %s", (title,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_successful_commit_returns_the_route_response(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    app = _build_app(app_url(grounded_db), seeded.a.tenant, seeded.a.admin)
    title = f"ok-{secrets.token_hex(4)}"
    with TestClient(app, raise_server_exceptions=False, backend_options=SELECTOR_LOOP) as client:
        response = client.post("/documents", params={"title": title})
    assert response.status_code == 200
    assert _stored_titles(grounded_db, title) == 1


def test_failed_commit_becomes_a_server_error_and_stores_nothing(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    app = _build_app(app_url(grounded_db), seeded.a.tenant, seeded.a.admin)
    title = f"dangling-{secrets.token_hex(4)}"
    with TestClient(app, raise_server_exceptions=False, backend_options=SELECTOR_LOOP) as client:
        response = client.post("/documents", params={"title": title, "dangling": "true"})
    assert response.status_code == 500, "a failed commit must not look like success"
    assert _stored_titles(grounded_db, title) == 0

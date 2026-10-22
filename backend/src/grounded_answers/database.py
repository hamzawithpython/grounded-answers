"""
Async database engine for the app role (ADR 0005).

The engine connects with DATABASE_URL, which is the app_user login. Migrations use a separate
URL (MIGRATION_DATABASE_URL), so the app never runs as the migration role or the superuser.
"""

import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from grounded_answers.runtime import ensure_psycopg_compatible_loop


def create_database_engine(database_url: str) -> AsyncEngine:
    """
    Build the async engine. Call it from inside a running event loop (the app lifespan).

    Fails with RuntimeError if the running loop is a Windows Proactor loop (ADR 0005).
    No connection is opened here; the first connection happens on the first query.
    """
    ensure_psycopg_compatible_loop(asyncio.get_running_loop())
    return create_async_engine(
        database_url,
        pool_size=5,
        max_overflow=10,
        # Detect dropped connections before a request uses them (ADR 0005).
        pool_pre_ping=True,
    )

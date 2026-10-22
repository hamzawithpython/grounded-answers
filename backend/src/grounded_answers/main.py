from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from grounded_answers.config import get_settings
from grounded_answers.database import create_database_engine
from grounded_answers.readiness import (
    ReadinessChecker,
    make_cache_check,
    make_database_check,
    make_schema_check,
)


def _make_lifespan(
    injected: ReadinessChecker | None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """
    Build the startup and shutdown hook.

    Production (injected is None): creates the database engine and the Valkey client once,
    shares them through app.state, and closes both on shutdown.

    Tests pass a ReadinessChecker with fake checks. Then the hook creates no connections,
    so the tests need no database or Valkey and run on any event loop.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if injected is not None:
            app.state.readiness = injected
            yield
            return

        settings = get_settings()
        engine = create_database_engine(str(settings.database_url))
        cache = Redis.from_url(str(settings.redis_url))
        app.state.readiness = ReadinessChecker(
            {
                "database": make_database_check(engine),
                "cache": make_cache_check(cache),
                "schema": make_schema_check(engine),
            }
        )
        try:
            yield
        finally:
            await engine.dispose()
            await cache.aclose()

    return lifespan


def create_app(readiness: ReadinessChecker | None = None) -> FastAPI:
    """
    Application factory function.

    Why a factory?
    - Tests can call create_app() multiple times with mocked dependencies
    - Each test gets a fresh, isolated app instance
    - Easy to create variants (e.g., app with/without middleware)
    - Avoids global state, making tests deterministic and parallelizable

    Pass `readiness` only in tests. Leave it None in production so the lifespan builds the
    real database engine and Valkey client.
    """
    app = FastAPI(
        title="Grounded Answers",
        description="Multi-tenant document Q&A assistant",
        version="0.1.0",
        lifespan=_make_lifespan(readiness),
    )

    settings = get_settings()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.get_cors_origins(),
        # Credentials let the browser send the refresh cookie (ADR 0004).
        # Origins must be exact (never "*") when credentials are allowed.
        allow_credentials=True,
        # Only the methods and headers the API actually uses. Extend when a new
        # endpoint needs one.
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        """
        Liveness probe: indicates the app is running and can respond to requests.

        Liveness vs Readiness:
        - Liveness (/health): Is the process alive? (no external dependencies)
        - Readiness (/ready): Can the app handle requests? (checks DB, cache, etc.)

        /health is used by container orchestrators to restart dead processes.
        /ready (added in Session 2) checks database and Valkey connectivity.
        """
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        """
        Readiness probe (ADR 0009). 200 when every dependency is up; 503 otherwise.

        The 503 body names only the failed dependency ("database", "cache" or "schema").
        Hostnames, error text and versions stay in the server log. The endpoint is public.
        """
        checker: ReadinessChecker = request.app.state.readiness
        failed = await checker.failed_dependencies()
        if failed:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "failed": list(failed)},
            )
        return JSONResponse(content={"status": "ready"})

    return app

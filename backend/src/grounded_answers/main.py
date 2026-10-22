from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from starlette.concurrency import run_in_threadpool

from grounded_answers.auth_api import router as auth_router
from grounded_answers.auth_keys import KeyFileError, load_key_ring
from grounded_answers.config import Settings, get_settings
from grounded_answers.database import create_database_engine, create_session_factory
from grounded_answers.jobs.logging_filter import install_job_log_redaction
from grounded_answers.passwords import make_dummy_hash
from grounded_answers.rate_limit import LoginLimiter, wall_clock_ms
from grounded_answers.readiness import (
    ReadinessChecker,
    make_cache_check,
    make_database_check,
    make_schema_check,
)
from grounded_answers.timing import TimingFloor


async def _load_auth_state(app: FastAPI, settings: Settings, cache: Redis) -> None:
    """
    Load what login needs before the app takes requests (ADR 0004).

    Each missing or malformed setting stops startup with a message that names the setting, never
    its value. The dummy hash is computed once here, in a worker thread (Argon2 is CPU-heavy).
    """
    if settings.auth_jwt_keys_file is None:
        raise RuntimeError("AUTH_JWT_KEYS_FILE is not set (docs/RUNBOOK.md, Auth keys)")
    active = settings.auth_jwt_active_kid
    try:
        app.state.key_ring = load_key_ring(settings.auth_jwt_keys_file, expected_active_kid=active)
    except KeyFileError as exc:
        raise RuntimeError(f"AUTH_JWT_KEYS_FILE is not usable: {exc}") from exc
    if settings.auth_log_hmac_key is None:
        raise RuntimeError("AUTH_LOG_HMAC_KEY is not set (docs/RUNBOOK.md, Auth keys)")
    secret = settings.auth_log_hmac_key.get_secret_value()
    if len(secret) < 32:
        raise RuntimeError("AUTH_LOG_HMAC_KEY must be at least 32 characters")
    app.state.auth_hmac_key = secret.encode("ascii")
    app.state.dummy_hash = await run_in_threadpool(make_dummy_hash)
    if settings.auth_rate_limit_hmac_key is None:
        raise RuntimeError("AUTH_RATE_LIMIT_HMAC_KEY is not set (docs/RUNBOOK.md, Auth keys)")
    rate_secret = settings.auth_rate_limit_hmac_key.get_secret_value()
    if len(rate_secret) < 32:
        raise RuntimeError("AUTH_RATE_LIMIT_HMAC_KEY must be at least 32 characters")
    app.state.cache = cache
    app.state.limiter = LoginLimiter(cache, rate_secret.encode("ascii"), wall_clock_ms)
    app.state.timing = TimingFloor(floor_seconds=settings.auth_login_floor_ms / 1000)


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
        app.state.session_factory = create_session_factory(engine)
        cache = Redis.from_url(str(settings.redis_url))
        app.state.readiness = ReadinessChecker(
            {
                "database": make_database_check(engine),
                "cache": make_cache_check(cache),
                "schema": make_schema_check(engine),
            }
        )
        try:
            await _load_auth_state(app, settings, cache)
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
    # The web app enqueues jobs, so its job log lines get the same argument redaction as the
    # worker's (ADR 0017). Idempotent: repeated factory calls add no second handler.
    install_job_log_redaction()
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

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """
        Return where and what was wrong, never the value that was sent (ADR 0004 note).

        FastAPI's default includes "input" for each error, so a bad password would be echoed back.
        Only the location, the error type and the message are kept.
        """
        errors = [
            {"type": err["type"], "loc": list(err["loc"]), "msg": err["msg"]}
            for err in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    app.include_router(auth_router)

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

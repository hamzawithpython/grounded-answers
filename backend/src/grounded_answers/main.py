from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from grounded_answers.config import get_settings


def create_app() -> FastAPI:
    """
    Application factory function.

    Why a factory?
    - Tests can call create_app() multiple times with mocked dependencies
    - Each test gets a fresh, isolated app instance
    - Easy to create variants (e.g., app with/without middleware)
    - Avoids global state, making tests deterministic and parallelizable
    """
    app = FastAPI(
        title="Grounded Answers",
        description="Multi-tenant document Q&A assistant",
        version="0.1.0",
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

    return app

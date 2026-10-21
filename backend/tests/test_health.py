from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from grounded_answers.config import get_settings
from grounded_answers.main import create_app


def test_health_endpoint() -> None:
    """GET /health returns 200 with ok status (liveness check)."""
    app = create_app()
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data: dict[str, str] = response.json()
    assert data == {"status": "ok"}


def test_cors_allowed_origin() -> None:
    """CORS headers present for allowed origins."""
    app = create_app()
    client = TestClient(app)
    response = client.get(
        "/health",
        headers={"Origin": "http://127.0.0.1:3000"},
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" in response.headers


def test_cors_allows_credentials_for_allowed_origin(monkeypatch: MonkeyPatch) -> None:
    """Allowed origin gets credentials header so the browser sends the refresh cookie."""
    monkeypatch.setenv("CORS_ORIGINS", "http://127.0.0.1:3000")
    get_settings.cache_clear()
    client = TestClient(create_app())
    response = client.get("/health", headers={"Origin": "http://127.0.0.1:3000"})
    assert response.headers.get("access-control-allow-origin") == "http://127.0.0.1:3000"
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_cors_rejects_localhost_when_only_127_0_0_1_configured(monkeypatch: MonkeyPatch) -> None:
    """localhost and 127.0.0.1 are different origins; only the configured one is allowed."""
    monkeypatch.setenv("CORS_ORIGINS", "http://127.0.0.1:3000")
    get_settings.cache_clear()
    client = TestClient(create_app())
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_never_wildcard_with_credentials(monkeypatch: MonkeyPatch) -> None:
    """A wildcard origin is never echoed back; credentials need exact origins."""
    monkeypatch.setenv("CORS_ORIGINS", "http://127.0.0.1:3000")
    get_settings.cache_clear()
    client = TestClient(create_app())
    response = client.get("/health", headers={"Origin": "http://evil.example.com"})
    assert response.headers.get("access-control-allow-origin") != "*"
    assert "access-control-allow-origin" not in response.headers


def test_cors_disallowed_origin() -> None:
    """CORS headers absent for disallowed origins (browser enforces)."""
    app = create_app()
    client = TestClient(app)
    response = client.get(
        "/health",
        headers={"Origin": "http://malicious.example.com"},
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers

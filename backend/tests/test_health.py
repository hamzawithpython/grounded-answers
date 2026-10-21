from fastapi.testclient import TestClient

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

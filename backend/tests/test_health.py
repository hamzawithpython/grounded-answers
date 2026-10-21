import pytest
from fastapi.testclient import TestClient

from grounded_answers.main import create_app


@pytest.fixture
def client() -> TestClient:
    """Create a fresh app instance for each test."""
    app = create_app()
    return TestClient(app)


def test_health_endpoint(client: TestClient) -> None:
    """GET /health returns 200 with ok status (liveness check)."""
    response = client.get("/health")
    assert response.status_code == 200
    data: dict[str, str] = response.json()
    assert data == {"status": "ok"}

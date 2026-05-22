from fastapi.testclient import TestClient

from app.main import app


def test_health_contract():
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["service"] == "server"
    assert body["version"] == "0.1.0"
    assert "llm_ready" in body
    assert "rag_ready" in body
    assert "memory_ready" in body
    assert "runtime_dir" in body

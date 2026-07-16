from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_ingest_route_and_health_rag_ready(tmp_path: Path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "worldview.md").write_text("# 世界\n避难所需要节约电力。", encoding="utf-8")
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=knowledge_dir,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        before = client.get("/health").json()
        assert before["rag_ready"] is True

        ingest = client.post("/ingest", json={"clear_first": True})
        assert ingest.status_code == 200
        assert ingest.json()["ok"] is True

        after = client.get("/health").json()
        assert after["rag_ready"] is True

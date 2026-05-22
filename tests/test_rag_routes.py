from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_rag_status_and_clear_routes(tmp_path: Path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "world.md").write_text("# 世界观\n外面有丧尸，庇护所是老师和 Mirdo 的安全据点。", encoding="utf-8")
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=knowledge_dir,
        embedding_provider="local_hash",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        before = client.get("/rag/status")
        assert before.status_code == 200
        assert before.json()["ready"] is False

        ingest = client.post("/ingest", json={"clear_first": True})
        assert ingest.status_code == 200
        assert ingest.json()["chunks_indexed"] >= 1

        ready = client.get("/rag/status")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True
        assert ready.json()["collection"] == "world_knowledge"

        clear = client.delete("/rag/clear")
        assert clear.status_code == 200
        assert clear.json()["ok"] is True
        assert client.get("/rag/status").json()["ready"] is False

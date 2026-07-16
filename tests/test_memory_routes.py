from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_session_history_snapshot_and_clear_contract(tmp_path: Path):
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=tmp_path / "knowledge",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        store = app.state.memory_store
        user_turn = store.add_turn("session-a", "user", "你好", {})
        store.add_turn("session-a", "assistant", "你好，老师。", {})
        fact = store.upsert_memory_fact("session-a", "player", "likes", "罐头汤", 0.8, user_turn.id)
        store.add_turn("session-b", "user", "另一个存档", {})

        sessions = client.get("/sessions")
        assert sessions.status_code == 200
        session_ids = {item["session_id"] for item in sessions.json()["sessions"]}
        assert {"session-a", "session-b"}.issubset(session_ids)

        history = client.get("/session/session-a/history?limit=5")
        assert history.status_code == 200
        history_body = history.json()
        assert history_body["ok"] is True
        assert len(history_body["turns"]) == 2

        snapshot = client.get("/session/session-a/snapshot")
        assert snapshot.status_code == 200
        snapshot_body = snapshot.json()
        assert snapshot_body["ok"] is True
        assert snapshot_body["memory_facts"][0]["value"] == "罐头汤"

        memory = client.get("/memory/session-a")
        assert memory.status_code == 200
        assert memory.json()["memory_facts"][0]["value"] == "罐头汤"

        search = client.get("/memory/session-a/search", params={"q": "喜欢什么", "limit": 5})
        assert search.status_code == 200
        assert any(item["value"] == "罐头汤" for item in search.json()["memory_facts"])

        delete_fact = client.delete(f"/memory/session-a/facts/{fact.id}")
        assert delete_fact.status_code == 200
        assert delete_fact.json()["deleted"] is True
        assert client.get("/memory/session-a").json()["memory_facts"] == []

        store.upsert_memory_fact("session-a", "player", "likes", "罐头汤", 0.8, user_turn.id)
        # Force the mutable memory Chroma collection to contain this fact, then ensure clear removes it too.
        app.state.memory_retriever.retrieve("session-a", "罐头汤", top_k=3)
        assert app.state.memory_retriever.count_session_vectors("session-a") >= 1

        clear = client.post("/memory/clear", json={"session_id": "session-a", "clear_all": False})
        assert clear.status_code == 200
        clear_body = clear.json()
        assert clear_body["ok"] is True
        assert "memory_index_deleted" in clear_body
        assert client.get("/session/session-a/history").json()["turns"] == []
        assert app.state.memory_retriever.count_session_vectors("session-a") == 0

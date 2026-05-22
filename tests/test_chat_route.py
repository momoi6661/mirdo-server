from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_chat_route_contract_with_fake_model(tmp_path: Path):
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="http://localhost:11434/v1",
        api_key="",
        chat_model="qwen3",
    )
    app = create_app(settings, chat_model_factory=lambda _resolved: _FakeChatModel(
    '{"dialogue":"你好，老师。","emotion":"平静","action":"Talk","stat_change":{"favor":1},"memory_tags":["greeting"]}'
    ))

    with TestClient(app) as client:
        response = client.post("/chat", json={"session_id": "s1", "player_text": "你好", "day": 1, "time_min": 540})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["dialogue"] == "你好，老师。"
    assert body["emotion"] == "平静"
    assert body["action"] == "listen"
    assert body["stat_change"]["favor"] == 1
    assert body["session_id"] == "s1"
    assert body["turn_id"] > 0


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChatModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.last_messages = None

    def invoke(self, messages):
        self.last_messages = messages
        return _FakeMessage(self.content)



def test_chat_route_uses_ingested_knowledge(tmp_path: Path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "xiaokong_persona.md").write_text(
        "# 小空\n小空会认真管理便利站库存，记得玩家喜欢罐头汤。",
        encoding="utf-8",
    )
    fake_model = _FakeChatModel('{"dialogue":"我记得，你喜欢罐头汤。","emotion":"温和","action":"Talk"}')
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=knowledge_dir,
        api_base_url="http://localhost:11434/v1",
        api_key="",
        chat_model="qwen3",
    )
    app = create_app(settings, chat_model_factory=lambda _resolved: fake_model)

    with TestClient(app) as client:
        ingest = client.post("/ingest", json={"clear_first": True})
        assert ingest.status_code == 200
        response = client.post("/chat", json={"session_id": "s1", "player_text": "你记得我喜欢什么吗？"})

    assert response.status_code == 200
    body = response.json()
    assert any("罐头汤" in hit["text"] for hit in body["used_knowledge"])
    assert any("罐头汤" in message[1] for message in fake_model.last_messages)

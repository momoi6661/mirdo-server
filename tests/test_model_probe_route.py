from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_model_probe_contract_with_fake_provider(tmp_path: Path):
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="http://localhost:11434/v1",
        api_key="",
        chat_model="qwen3",
    )
    app = create_app(settings, chat_model_factory=lambda _resolved: _FakeChatModel("pong"))

    with TestClient(app) as client:
        response = client.get("/model/probe")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["model"] == "qwen3"
    assert body["content_preview"] == "pong"


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChatModel:
    def __init__(self, content: str) -> None:
        self.content = content

    def invoke(self, _messages):
        return _FakeMessage(self.content)

from pathlib import Path

from app.config import Settings, get_settings


def test_settings_defaults_use_local_runtime_paths(monkeypatch):
    monkeypatch.delenv("APP_HOST", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)
    monkeypatch.delenv("API_BASE_URL", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("CHAT_MODEL", raising=False)

    settings = Settings()

    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 5678
    assert settings.service_name == "mirdo-server"
    assert settings.runtime_dir == Path("data/runtime")
    assert settings.conversation_db == Path("data/runtime/conversations.sqlite3")
    assert settings.rag_db == Path("data/runtime/rag.sqlite3")
    assert settings.knowledge_dir == Path("data/knowledge")
    assert settings.chat_max_tokens == 0


def test_settings_llm_ready_requires_base_url_key_and_model():
    missing_key = Settings(api_base_url="https://api.openai.com/v1", api_key="", chat_model="gpt-4o-mini")
    assert missing_key.llm_ready is False

    ready = Settings(api_base_url="https://api.openai.com/v1", api_key="sk-test", chat_model="gpt-4o-mini")
    assert ready.llm_ready is True


def test_get_settings_is_cached():
    first = get_settings()
    second = get_settings()
    assert first is second

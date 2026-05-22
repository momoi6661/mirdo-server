from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "server"
    version: str = "0.1.0"

    app_host: str = "127.0.0.1"
    app_port: int = 5678
    app_reload: bool = False

    api_base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    chat_model: str = "gpt-4o-mini"
    # Optional HTTP proxy for outbound OpenAI-compatible requests.
    # Can be overridden by Godot user://ai_settings.cfg or request.provider.proxy_url.
    proxy_url: str = ""

    knowledge_dir: Path = Path("data/knowledge")
    runtime_dir: Path = Path("data/runtime")
    conversation_db: Path = Path("data/runtime/conversations.sqlite3")
    chroma_dir: Path = Path("data/runtime/chroma")

    top_k: int = Field(default=4, ge=1, le=20)
    temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    request_timeout: float = Field(default=45.0, gt=0.0)
    chat_max_tokens: int = Field(default=240, ge=32, le=4096)
    context_window_turns: int = Field(default=8, ge=0, le=50)
    embedding_provider: str = "fastembed"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_cache_dir: str = "data/models/fastembed"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    rag_include_project_tree: bool = False

    @computed_field
    @property
    def llm_ready(self) -> bool:
        return bool(
            self.api_base_url.strip()
            and self.api_key.strip()
            and self.chat_model.strip()
        )

    def ensure_runtime_dirs(self) -> None:
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.conversation_db.parent.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

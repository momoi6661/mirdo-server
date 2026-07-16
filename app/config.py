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

    service_name: str = "mirdo-server"
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
    # 路径名保留兼容性；当前运行时使用 SQLite FTS5，不再打开 Chroma。
    rag_db: Path = Path("data/runtime/rag.sqlite3")

    top_k: int = Field(default=4, ge=1, le=20)
    temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    request_timeout: float = Field(default=45.0, gt=0.0)
    # 0 表示不发送 ``max_tokens``，让兼容服务商按模型默认值决定推理与输出预算。
    chat_max_tokens: int = Field(default=0, ge=0, le=4096)
    # Agent 默认启用 tools：知识、记忆与 Godot 可执行能力都通过工具取得。
    model_tools_enabled: bool = True
    # 本地联调默认记录 Chat 的输入、上下文摘要和输出；provider/API Key 永不写入日志。
    chat_trace_enabled: bool = True
    context_window_turns: int = Field(default=8, ge=0, le=50)
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
        self.rag_db.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

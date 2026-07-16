from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TTSSettings(BaseSettings):
    """VOICEVOX 的配置。

    这些配置使用 ``TTS_`` 前缀，例如 ``TTS_ENGINE_URL``，不会污染主聊天
    服务的 LLM 配置。TTS 会作为主后端里的一个独立 Provider 使用。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TTS_",
        extra="ignore",
    )

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=5680, ge=1, le=65535)
    provider: str = "voicevox"
    engine_url: str = "http://127.0.0.1:50021"
    # 留空时使用角色配置文件里的 speaker_id；只有调试时才建议覆盖它。
    speaker_id: int | None = Field(default=None, ge=0)
    request_timeout: float = Field(default=30.0, gt=0.0, le=300.0)
    cache_enabled: bool = True
    cache_dir: Path = Path("data/runtime/tts")
    # 角色声线和对话文本分开保存，便于以后增加角色或语言。
    profile_dir: Path = Path("data/tts/characters")
    dialogue_dir: Path = Path("data/dialogue")

    def ensure_dirs(self) -> None:
        """只创建 TTS 自己的缓存目录，不触碰基础服务目录。"""
        if self.cache_enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_tts_settings() -> TTSSettings:
    return TTSSettings()

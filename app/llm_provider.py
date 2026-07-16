"""模型服务商配置解析。

这里故意不实现 HTTP 客户端、消息格式或 JSON 修复；这些都是 PydanticAI 的职责。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import time
from urllib.parse import urlsplit, urlunsplit
from typing import Any

from .config import Settings
from .schemas import ProviderConfig


class ProviderResolutionError(ValueError):
    """模型连接信息不完整。"""


@dataclass(frozen=True)
class ResolvedProvider:
    """交给 PydanticAI OpenAIProvider 的最小连接配置。"""

    base_url: str
    api_key: str
    model: str
    proxy_url: str = ""


class LLMProvider:
    """按请求、Godot 配置、本地环境的顺序解析模型配置。"""

    LEGACY_GODOT_APP_NAMES = ("Mirdo", "24h")
    GODOT_SETTINGS_CACHE_TTL_SECONDS = 2.0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._godot_settings_cache: tuple[float, dict[str, str]] | None = None

    def resolve_provider(self, request_provider: ProviderConfig | None = None) -> ResolvedProvider:
        """返回统一的 OpenAI-compatible 配置，不创建任何模型客户端。"""
        requested = request_provider or ProviderConfig()
        godot = self._load_godot_ai_settings()
        base_url = self._first(requested.base_url, godot.get("base_url"), self.settings.api_base_url)
        api_key = self._first(requested.api_key, godot.get("api_key"), self.settings.api_key)
        model = self._first(requested.model, godot.get("model"), self.settings.chat_model)
        proxy_url = self._first(requested.proxy_url, godot.get("proxy_url"), self.settings.proxy_url)
        base_url = self._normalize_openai_base_url(base_url)
        if self._is_local_base_url(base_url):
            # 本地模型网关不应再走 HTTP 代理，否则会变成“本地 -> 本地代理 -> 本地网关”的绕路。
            proxy_url = ""
        if not base_url:
            raise ProviderResolutionError("provider base_url is required; set it in Godot AISettings")
        if not model:
            raise ProviderResolutionError("provider model is required; set it in Godot AISettings")
        return ResolvedProvider(base_url=base_url, api_key=api_key, model=model, proxy_url=proxy_url)


    def _normalize_openai_base_url(self, value: str) -> str:
        """补齐 OpenAI-compatible 常见的 /v1 路径。

        很多本地转发器、LM Studio、Ollama OpenAI 兼容端口都要求 SDK 访问
        ``/v1/chat/completions``。用户在 Godot 里常只填 ``http://host:port``；
        这里自动补成 ``http://host:port/v1``，避免请求打到 ``/chat/completions`` 404。
        """
        clean = value.rstrip("/")
        parsed = urlsplit(clean)
        if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.path in {"", "/"}:
            return urlunsplit((parsed.scheme, parsed.netloc, "/v1", "", ""))
        return clean


    def _is_local_base_url(self, value: str) -> bool:
        """判断模型服务是否是本机网关；本机网关不需要再套代理。"""
        host = urlsplit(value).hostname or ""
        return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

    def _first(self, *values: Any) -> str:
        for value in values:
            clean = self._clean_config_value(value)
            if clean:
                return clean
        return ""

    def _clean_config_value(self, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        while len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            text = text[1:-1].strip()
        return text

    def _load_godot_ai_settings(self) -> dict[str, str]:
        now = time.monotonic()
        if self._godot_settings_cache and now - self._godot_settings_cache[0] <= self.GODOT_SETTINGS_CACHE_TTL_SECONDS:
            return dict(self._godot_settings_cache[1])
        for path in self._godot_ai_settings_paths():
            values = self._parse_godot_ai_settings_file(path)
            if values:
                self._godot_settings_cache = (now, values)
                return dict(values)
        self._godot_settings_cache = (now, {})
        return {}

    def _parse_godot_ai_settings_file(self, path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        result: dict[str, str] = {}
        section = ""
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith((";", "#")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].strip()
                elif section == "provider" and "=" in line:
                    key, value = (part.strip() for part in line.split("=", 1))
                    if key in {"base_url", "api_key", "model", "proxy_url"}:
                        result[key] = self._clean_config_value(value)
        except OSError:
            return {}
        return result

    def _godot_ai_settings_paths(self) -> list[Path]:
        appdata = os.environ.get("APPDATA", "").strip()
        root = Path(appdata) / "Godot" / "app_userdata" if appdata else Path.home() / "AppData" / "Roaming" / "Godot" / "app_userdata"
        names = [self._godot_project_name(), *self.LEGACY_GODOT_APP_NAMES]
        return [root / name / "ai_settings.cfg" for name in dict.fromkeys(name for name in names if name)]

    def _godot_project_name(self) -> str:
        project_path = Path(__file__).resolve().parents[2].parent / "FPS" / "project.godot"
        try:
            for raw in project_path.read_text(encoding="utf-8").splitlines():
                if raw.strip().startswith("config/name") and "=" in raw:
                    return self._clean_config_value(raw.split("=", 1)[1])
        except OSError:
            pass
        return ""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import os
import time
from typing import Any

import httpx
from langchain_openai import ChatOpenAI

from .config import Settings
from .schemas import ProviderConfig


class ProviderResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedProvider:
    base_url: str
    api_key: str
    model: str
    proxy_url: str = ""


ChatModelFactory = Callable[[ResolvedProvider], Any]


class OpenAICompatibleHTTPMessage:
    def __init__(self, content: str, response_metadata: dict[str, Any] | None = None) -> None:
        self.content = content
        self.response_metadata = response_metadata or {}
        self.additional_kwargs: dict[str, Any] = {}


class OpenAICompatibleHTTPChatModel:
    def __init__(
        self,
        resolved: ResolvedProvider,
        *,
        temperature: float,
        max_tokens: int,
        timeout: float,
        json_mode: bool = False,
    ) -> None:
        self.resolved = resolved
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.json_mode = json_mode
        self._client: httpx.Client | None = None

    def invoke(self, messages: list[tuple[str, str]] | list[Any]) -> OpenAICompatibleHTTPMessage:
        payload: dict[str, Any] = {
            "model": self.resolved.model,
            "messages": self._format_messages(messages),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": "Bearer %s" % (self.resolved.api_key or "not-needed"),
            "Content-Type": "application/json",
        }
        response = self._http_client().post(
            self.resolved.base_url.rstrip("/") + "/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        content = ""
        if isinstance(message, dict):
            content = str(message.get("content") or "")
            # Some NVIDIA-hosted reasoning models may put visible text in reasoning_content
            # when max_tokens is tiny; keep content primary to preserve normal chat behavior.
            if not content and message.get("reasoning_content"):
                content = str(message.get("reasoning_content") or "")
        metadata = {
            "token_usage": data.get("usage", {}),
            "model_name": data.get("model", self.resolved.model),
            "id": data.get("id", ""),
            "finish_reason": choice.get("finish_reason", "") if isinstance(choice, dict) else "",
            "raw_response": data,
        }
        return OpenAICompatibleHTTPMessage(content, metadata)

    def _http_client(self) -> httpx.Client:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "timeout": self.timeout,
                "trust_env": False,
            }
            if self.resolved.proxy_url:
                kwargs["proxy"] = self.resolved.proxy_url
            self._client = httpx.Client(**kwargs)
        return self._client

    def _format_messages(self, messages: list[tuple[str, str]] | list[Any]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for item in messages:
            if isinstance(item, tuple) and len(item) >= 2:
                result.append({"role": str(item[0]), "content": str(item[1])})
            elif isinstance(item, dict):
                result.append({"role": str(item.get("role", "user")), "content": str(item.get("content", ""))})
            else:
                role = str(getattr(item, "type", getattr(item, "role", "user")))
                content = str(getattr(item, "content", item))
                if role == "human":
                    role = "user"
                elif role == "ai":
                    role = "assistant"
                result.append({"role": role, "content": content})
        return result


class LLMProvider:
    LEGACY_GODOT_APP_NAMES = ("Mirdo", "24h")
    GODOT_SETTINGS_CACHE_TTL_SECONDS = 2.0

    def __init__(self, settings: Settings, chat_model_factory: ChatModelFactory | None = None) -> None:
        self.settings = settings
        self._uses_default_chat_model_factory = chat_model_factory is None
        self._chat_model_factory = chat_model_factory or self._default_chat_model_factory
        self._godot_settings_cache: tuple[float, dict[str, str]] | None = None
        self._model_cache: dict[tuple[str, str, str, str, int | None, float | None, bool], Any] = {}

    def resolve_provider(self, request_provider: ProviderConfig | None = None) -> ResolvedProvider:
        base_url = ""
        api_key = ""
        model = ""
        proxy_url = ""

        if request_provider is not None:
            base_url = self._clean_config_value(request_provider.base_url)
            api_key = self._clean_config_value(request_provider.api_key)
            model = self._clean_config_value(request_provider.model)
            proxy_url = self._clean_config_value(request_provider.proxy_url)

        godot_provider = self._load_godot_ai_settings()

        if not base_url:
            base_url = self._clean_config_value(godot_provider.get("base_url", ""))
        if not api_key:
            api_key = self._clean_config_value(godot_provider.get("api_key", ""))
        if not model:
            model = self._clean_config_value(godot_provider.get("model", ""))
        if not proxy_url:
            proxy_url = self._clean_config_value(godot_provider.get("proxy_url", ""))

        if not base_url:
            base_url = self._clean_config_value(self.settings.api_base_url)
        if not api_key:
            api_key = self._clean_config_value(self.settings.api_key)
        if not model:
            model = self._clean_config_value(self.settings.chat_model)
        if not proxy_url:
            proxy_url = self._clean_config_value(self.settings.proxy_url)

        if not base_url:
            raise ProviderResolutionError("provider base_url is required; set it in Godot AISettings")
        if not model:
            raise ProviderResolutionError("provider model is required; set it in Godot AISettings")

        return ResolvedProvider(base_url=base_url.rstrip("/"), api_key=api_key, model=model, proxy_url=proxy_url)

    def _clean_config_value(self, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        # Godot ConfigFile stores strings as quoted values. Some non-Godot probes/tests
        # read the cfg with INI parsers and may pass those quotes through to the backend.
        while len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '\"'}:
            text = text[1:-1].strip()
        return text

    def _load_godot_ai_settings(self) -> dict[str, str]:
        if not self._uses_default_chat_model_factory:
            return {}
        now = time.monotonic()
        if self._godot_settings_cache is not None:
            cached_at, cached = self._godot_settings_cache
            if now - cached_at <= self.GODOT_SETTINGS_CACHE_TTL_SECONDS:
                return dict(cached)

        candidates = self._godot_ai_settings_paths()
        loaded: list[tuple[Path, dict[str, str]]] = []
        for path in candidates:
            if not path.exists():
                continue
            parsed = self._parse_godot_ai_settings_file(path)
            if parsed:
                loaded.append((path, parsed))
        if not loaded:
            self._godot_settings_cache = (now, {})
            return {}

        # Prefer the active project name. If several historical app_userdata folders exist,
        # prefer the one that actually contains proxy_url so the UI setting is respected.
        selected_path, selected = loaded[0]
        for path, parsed in loaded:
            if parsed.get("proxy_url", ""):
                selected_path, selected = path, parsed
                break
        print(
            "[LLMProvider] loaded Godot AISettings path=%s proxy=%s"
            % (selected_path, "enabled" if selected.get("proxy_url", "") else "disabled"),
            flush=True,
        )
        self._godot_settings_cache = (now, dict(selected))
        return dict(selected)

    def _parse_godot_ai_settings_file(self, path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        current_section = ""
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith(";") or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    current_section = line[1:-1].strip()
                    continue
                if current_section != "provider" or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key in {"base_url", "api_key", "model", "proxy_url"}:
                    result[key] = self._clean_config_value(value)
        except OSError:
            return {}
        return result

    def _godot_ai_settings_paths(self) -> list[Path]:
        appdata = os.environ.get("APPDATA", "").strip()
        root = Path(appdata) / "Godot" / "app_userdata" if appdata else Path.home() / "AppData" / "Roaming" / "Godot" / "app_userdata"
        names: list[str] = []
        project_name = self._godot_project_name()
        if project_name:
            names.append(project_name)
        names.extend(self.LEGACY_GODOT_APP_NAMES)

        result: list[Path] = []
        seen: set[str] = set()
        for name in names:
            clean = str(name).strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            result.append(root / clean / "ai_settings.cfg")
        return result

    def _godot_project_name(self) -> str:
        # Server lives next to FPS: D:/AAgodot/Server/app -> D:/AAgodot/FPS/project.godot
        project_path = Path(__file__).resolve().parents[2].parent / "FPS" / "project.godot"
        try:
            for raw_line in project_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if line.startswith("config/name") and "=" in line:
                    return self._clean_config_value(line.split("=", 1)[1])
        except OSError:
            return ""
        return ""

    def build_chat_model(
        self,
        request_provider: ProviderConfig | None = None,
        *,
        max_tokens: int | None = None,
        timeout: float | None = None,
        json_mode: bool = False,
        ) -> Any:
        resolved = self.resolve_provider(request_provider)
        cache_key = (
            resolved.base_url,
            resolved.api_key,
            resolved.model,
            resolved.proxy_url,
            max_tokens,
            timeout,
            json_mode,
        )
        if cache_key not in self._model_cache:
            if not self._uses_default_chat_model_factory:
                self._model_cache[cache_key] = self._chat_model_factory(resolved)
            elif max_tokens is None and timeout is None and not json_mode:
                self._model_cache[cache_key] = self._chat_model_factory(resolved)
            else:
                self._model_cache[cache_key] = self._default_chat_model_factory(
                    resolved,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    json_mode=json_mode,
                )
        return self._model_cache[cache_key]

    def probe_model(self, request_provider: ProviderConfig | None = None) -> dict[str, Any]:
        try:
            resolved = self.resolve_provider(request_provider)
            if self._uses_default_chat_model_factory:
                chat_model = self.build_chat_model(
                    request_provider,
                    max_tokens=1,
                    timeout=min(max(float(self.settings.request_timeout), 8.0), 20.0),
                    json_mode=False,
                )
            else:
                chat_model = self.build_chat_model(request_provider)
            message = chat_model.invoke([
                ("user", "1"),
            ])
            content = str(getattr(message, "content", "") or "").strip()
            if not content:
                return {
                    "ok": False,
                    "error": "empty_model_content",
                    "base_url": resolved.base_url,
                    "model": resolved.model,
                    "content_preview": "",
                }
            return {
                "ok": True,
                "base_url": resolved.base_url,
                "model": resolved.model,
                "proxy_enabled": bool(resolved.proxy_url),
                "content_preview": content[:120],
            }
        except Exception as exc:
            godot_provider = self._load_godot_ai_settings()
            model = request_provider.model if request_provider is not None and request_provider.model else (godot_provider.get("model", "") or self.settings.chat_model)
            base_url = request_provider.base_url if request_provider is not None and request_provider.base_url else (godot_provider.get("base_url", "") or self.settings.api_base_url)
            return {
                "ok": False,
                "error": self._redact_secret(str(exc)),
                "base_url": str(base_url).strip().rstrip("/"),
                "model": str(model).strip(),
                "proxy_enabled": bool(godot_provider.get("proxy_url", "")),
                "content_preview": "",
            }

    def _default_chat_model_factory(
        self,
        resolved: ResolvedProvider,
        *,
        max_tokens: int | None = None,
        timeout: float | None = None,
        json_mode: bool = False,
    ) -> ChatOpenAI:
        return OpenAICompatibleHTTPChatModel(
            resolved,
            temperature=self.settings.temperature,
            timeout=timeout if timeout is not None else max(float(self.settings.request_timeout), 90.0),
            max_tokens=max_tokens or self.settings.chat_max_tokens,
            json_mode=json_mode,
        )

    def _langchain_chat_model_factory(
        self,
        resolved: ResolvedProvider,
        *,
        max_tokens: int | None = None,
        timeout: float | None = None,
        json_mode: bool = False,
    ) -> ChatOpenAI:
        # OpenAI-compatible local servers such as Ollama/LM Studio often accept an empty key,
        # but langchain-openai still expects a non-empty API key string.
        api_key = resolved.api_key or "not-needed"
        model_kwargs: dict[str, Any] = {}
        if json_mode:
            model_kwargs["response_format"] = {"type": "json_object"}
        kwargs: dict[str, Any] = {
            "model": resolved.model,
            "api_key": api_key,
            "base_url": resolved.base_url,
            "temperature": self.settings.temperature,
            "timeout": timeout if timeout is not None else max(float(self.settings.request_timeout), 90.0),
            "max_tokens": max_tokens or self.settings.chat_max_tokens,
        }
        if resolved.proxy_url:
            kwargs["openai_proxy"] = resolved.proxy_url
            print(
                "[LLMProvider] using provider base_url=%s model=%s proxy=enabled" % (resolved.base_url, resolved.model),
                flush=True,
            )
        else:
            print(
                "[LLMProvider] using provider base_url=%s model=%s proxy=disabled" % (resolved.base_url, resolved.model),
                flush=True,
            )
        if model_kwargs:
            kwargs["model_kwargs"] = model_kwargs
        return ChatOpenAI(**kwargs)

    def _redact_secret(self, text: str) -> str:
        redacted = text
        for secret in [self.settings.api_key, "not-needed"]:
            if secret:
                redacted = redacted.replace(secret, "***")
        return redacted




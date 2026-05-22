from app.config import Settings
from app.llm_provider import LLMProvider, ProviderResolutionError, OpenAICompatibleHTTPChatModel
from app.schemas import ProviderConfig


def test_resolve_provider_prefers_request_provider():
    settings = Settings(api_base_url="https://default.example/v1", api_key="default-key", chat_model="default-model")
    provider = LLMProvider(settings)

    resolved = provider.resolve_provider(
        ProviderConfig(base_url=" https://request.example/v1/ ", api_key=" request-key ", model=" request-model ")
    )

    assert resolved.base_url == "https://request.example/v1"
    assert resolved.api_key == "request-key"
    assert resolved.model == "request-model"


def test_resolve_provider_falls_back_to_settings():
    settings = Settings(api_base_url="https://default.example/v1", api_key="default-key", chat_model="default-model")
    provider = LLMProvider(settings, chat_model_factory=lambda _resolved: _FakeChatModel("pong"))

    resolved = provider.resolve_provider(None)

    assert resolved.base_url == "https://default.example/v1"
    assert resolved.api_key == "default-key"
    assert resolved.model == "default-model"


def test_resolve_provider_requires_base_url_and_model_but_allows_empty_key_for_local_models():
    settings = Settings(api_base_url="", api_key="", chat_model="")
    provider = LLMProvider(settings, chat_model_factory=lambda _resolved: _FakeChatModel("pong"))

    try:
        provider.resolve_provider(None)
    except ProviderResolutionError as exc:
        assert "model" in str(exc) or "base_url" in str(exc)
    else:
        raise AssertionError("missing provider should fail")

    resolved = provider.resolve_provider(ProviderConfig(base_url="http://localhost:11434/v1", api_key="", model="qwen3"))
    assert resolved.api_key == ""


def test_probe_model_with_fake_client_success():
    settings = Settings(api_base_url="http://localhost:11434/v1", api_key="", chat_model="qwen3")
    provider = LLMProvider(settings, chat_model_factory=lambda _resolved: _FakeChatModel("pong"))

    result = provider.probe_model()

    assert result["ok"] is True
    assert result["model"] == "qwen3"
    assert result["content_preview"] == "pong"


def test_probe_model_reports_errors_without_leaking_api_key():
    settings = Settings(api_base_url="https://example.test/v1", api_key="secret-key", chat_model="model-a")

    def raise_error(_resolved):
        raise RuntimeError("boom secret-key")

    provider = LLMProvider(settings, chat_model_factory=raise_error)
    result = provider.probe_model()

    assert result["ok"] is False
    assert result["model"] == "model-a"
    assert "secret-key" not in result["error"]
    assert "***" in result["error"]


def test_default_chat_model_uses_lightweight_http_client_for_provider():
    settings = Settings(api_base_url="https://example.test/v1", api_key="secret-key", chat_model="model-a")
    provider = LLMProvider(settings)

    model = provider.build_chat_model(
        ProviderConfig(
            base_url="https://example.test/v1",
            api_key="secret-key",
            model="model-a",
            proxy_url="http://127.0.0.1:7890",
        )
    )

    assert isinstance(model, OpenAICompatibleHTTPChatModel)
    assert model.resolved.proxy_url == "http://127.0.0.1:7890"


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChatModel:
    def __init__(self, content: str) -> None:
        self.content = content

    def invoke(self, _messages):
        return _FakeMessage(self.content)


def test_probe_model_default_factory_uses_tiny_token_budget():
    settings = Settings(api_base_url="https://example.test/v1", api_key="secret-key", chat_model="model-a")
    provider = LLMProvider(settings)
    calls = []

    class TinyProbeModel:
        def invoke(self, messages):
            calls.append(messages)
            return _FakeMessage("pong")

    def factory(resolved, *, max_tokens=None, timeout=None, json_mode=False):
        calls.append({"max_tokens": max_tokens, "timeout": timeout, "json_mode": json_mode})
        return TinyProbeModel()

    provider._default_chat_model_factory = factory

    result = provider.probe_model()

    assert result["ok"] is True
    assert calls[0]["max_tokens"] == 1
    assert calls[0]["json_mode"] is False
    assert calls[0]["timeout"] <= 20.0
    assert calls[1] == [("user", "1")]

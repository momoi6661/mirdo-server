from pydantic import ValidationError

from app.schemas import ChatRequest, NpcStats, ProviderConfig


def test_chat_request_defaults_session_and_time_min():
    request = ChatRequest(player_text="  你好  ", time=540)

    assert request.session_id == "default_session"
    assert request.player_text == "你好"
    assert request.effective_time_min() == 540
    assert request.npc_stats == NpcStats()


def test_chat_request_prefers_time_min_over_time():
    request = ChatRequest(player_text="你好", time=100, time_min=250)
    assert request.effective_time_min() == 250


def test_provider_config_trims_values_and_strips_base_url_slash():
    provider = ProviderConfig(
        base_url=" http://localhost:11434/v1/ ",
        api_key=" key ",
        model=" qwen3 ",
    )

    assert provider.base_url == "http://localhost:11434/v1"
    assert provider.api_key == "key"
    assert provider.model == "qwen3"


def test_empty_player_text_is_rejected():
    try:
        ChatRequest(player_text="   ")
    except ValidationError as exc:
        assert "player_text" in str(exc)
    else:
        raise AssertionError("empty player_text should fail validation")

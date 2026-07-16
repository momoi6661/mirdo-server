from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from app.tts.config import TTSSettings
from app.tts.chat import attach_tts_to_response
from app.tts.models import TTSSynthesisRequest
from app.tts.profiles import MirdoVoiceProfile
from app.tts.service import TTSService
from app.tts.voicevox import VoicevoxClient
from app.schemas import ChatRequest, ChatResponse


def test_voicevox_adapter_uses_audio_query_then_synthesis() -> None:
    """适配器只负责 VOICEVOX 协议，且会把显式参数写回查询结果。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/audio_query":
            return httpx.Response(200, json={"speedScale": 1.0, "pitchScale": 0.0})
        if request.url.path == "/synthesis":
            return httpx.Response(200, content=b"RIFF-test-wav")
        return httpx.Response(404)

    async def run() -> bytes:
        provider = VoicevoxClient(engine_url="http://voicevox.test", timeout=5)
        await provider._client.aclose()  # 测试替换为内存 Transport，不访问网络。
        provider._client = httpx.AsyncClient(
            base_url="http://voicevox.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            return await provider.query_and_synthesize(
                TTSSynthesisRequest(text="こんにちは", speed_scale=1.1),
                speaker_id=3,
            )
        finally:
            await provider.close()

    assert asyncio.run(run()) == b"RIFF-test-wav"
    assert [request.url.path for request in seen] == ["/audio_query", "/synthesis"]
    assert b'"speedScale":1.1' in seen[1].content


def test_tts_service_caches_same_request(tmp_path) -> None:
    """缓存命中时不应再次调用上游 Engine。"""
    calls = 0

    class FakeVoicevox:
        async def query_and_synthesize(self, request: TTSSynthesisRequest, *, speaker_id: int) -> bytes:
            nonlocal calls
            calls += 1
            return b"RIFF" + b"-test-wav" * 8

        async def close(self) -> None:
            return None

    async def run() -> tuple[bool, bool]:
        service = TTSService(TTSSettings(cache_dir=tmp_path))
        service.voicevox = FakeVoicevox()  # type: ignore[assignment]
        request = TTSSynthesisRequest(text="こんにちは")
        try:
            first = await service.synthesize(request)
            second = await service.synthesize(request)
            return first.cache_hit, second.cache_hit
        finally:
            await service.close()

    first_hit, second_hit = asyncio.run(run())
    assert (first_hit, second_hit) == (False, True)
    assert calls == 1


def test_mirdo_voice_profile_keeps_emotion_parameters_bounded() -> None:
    """人格层只输出预设范围内的参数，不让模型直接破坏声线。"""
    profile = MirdoVoiceProfile()
    request = profile.apply(TTSSynthesisRequest(text="こんにちは", emotion="害羞", emotion_intensity=0.8))
    assert request.speaker_id == 20
    assert 0.5 <= request.speed_scale <= 2.0
    assert -0.15 <= request.pitch_scale <= 0.15
    assert 0.0 <= request.intonation_scale <= 2.0


def test_chat_can_disable_tts_without_calling_provider() -> None:
    """请求显式关闭时，Agent 仍返回文字，但不会触发音频生成。"""

    class FailingService:
        settings = SimpleNamespace(provider="voicevox")

        async def synthesize(self, _request):
            raise AssertionError("provider should not be called")

    async def run() -> ChatResponse:
        response = ChatResponse(dialogue="こんにちは")
        return await attach_tts_to_response(
            FailingService(),
            ChatRequest(player_text="你好", use_tts=False),
            response,
        )

    response = asyncio.run(run())
    assert response.tts.requested is False
    assert response.tts.generated is False


def test_chat_request_does_not_enable_tts_implicitly() -> None:
    """默认聊天不连接 VOICEVOX，只有请求明确传 true 才启用。"""

    assert ChatRequest(player_text="你好").use_tts is False


def test_chat_response_uses_agent_dialogue_and_emotion_for_tts() -> None:
    """默认开启时，Agent 的对白和情绪会进入统一 TTS Provider。"""
    calls: list[TTSSynthesisRequest] = []

    class FakeService:
        settings = SimpleNamespace(provider="voicevox")

        async def synthesize(self, request: TTSSynthesisRequest):
            calls.append(request)
            from app.tts.models import TTSResult

            return TTSResult(path="demo.wav", cache_key="a" * 32, cache_hit=False, profile_id="mirdo_ja")

    async def run() -> ChatResponse:
        return await attach_tts_to_response(
            FakeService(),
            ChatRequest(player_text="你好", use_tts=True),
            ChatResponse(dialogue="おかえり。", emotion="温柔"),
        )

    response = asyncio.run(run())
    assert response.tts.requested is True
    assert response.tts.generated is True
    assert response.tts.audio_url == "/tts/audio/" + "a" * 32
    assert calls[0].text == "おかえり。"
    assert calls[0].emotion == "温柔"

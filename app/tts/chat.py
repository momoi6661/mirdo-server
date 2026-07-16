from __future__ import annotations

import logging
from time import perf_counter

from ..schemas import ChatRequest, ChatResponse, TTSOutput
from .models import TTSSynthesisRequest
from .service import TTSService
from .voicevox import VoicevoxError


logger = logging.getLogger(__name__)


async def attach_tts_to_response(
    service: TTSService | None,
    request: ChatRequest,
    response: ChatResponse,
) -> ChatResponse:
    """把 Agent/Graph 的最终对白转换为可播放音频元数据。

    TTS 属于输出呈现，不作为 Agent tool 让模型“猜测是否播放”。Agent 负责
    生成 ``dialogue`` 和 ``emotion``，这里按请求选项稳定执行；引擎失败只记录
    在 ``tts.error``，不会让文字聊天失败。
    """

    profile = request.tts_voice_profile.strip() or "mirdo_ja"
    if not request.use_tts:
        response.tts = TTSOutput(requested=False, voice_profile=profile)
        return response

    response.tts = TTSOutput(requested=True, voice_profile=profile)
    if not response.dialogue.strip():
        response.tts.error = "dialogue_is_empty"
        return response
    if service is None:
        response.tts.error = "tts_service_disabled"
        return response

    try:
        started = perf_counter()
        tts_text = response.dialogue
        text_source = "dialogue"
        if request.generate_japanese and response.dialogue_ja.strip():
            tts_text = response.dialogue_ja
            text_source = "dialogue_ja"
        result = await service.synthesize(
            TTSSynthesisRequest(
                text=tts_text,
                voice_profile=profile,
                emotion=response.emotion,
                speaker_id=request.tts_speaker_id,
            )
        )
    except VoicevoxError as exc:
        logger.warning("tts_failed provider=voicevox error=%s", exc)
        response.tts.error = str(exc)
        return response
    except Exception as exc:  # noqa: BLE001 - TTS 失败不能阻断 Agent 文字响应
        logger.exception("tts_failed_unexpected")
        response.tts.error = f"{exc.__class__.__name__}: {exc}"
        return response

    response.tts.generated = True
    response.tts.provider = service.settings.provider
    response.tts.text_source = text_source
    response.tts.audio_url = f"/tts/audio/{result.cache_key}"
    response.tts.cache_key = result.cache_key
    response.tts.cache_hit = result.cache_hit
    logger.info(
        "tts_ready cache=%s hit=%s chars=%d elapsed_ms=%.1f",
        result.cache_key,
        result.cache_hit,
        len(tts_text),
        (perf_counter() - started) * 1000,
    )
    return response

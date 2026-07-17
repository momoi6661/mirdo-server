from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
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
                emotion_intensity=response.emotion_intensity,
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
    requested_delivery = _requested_audio_delivery(request)
    if requested_delivery == "url":
        response.tts.audio_delivery = "url"
    elif await _attach_inline_audio_if_selected(request, response, result.path):
        response.tts.audio_delivery = "inline"
    else:
        # 这里不是 Godot 的“失败后回退”，而是后端根据体积/读取结果明确选择 URL。
        response.tts.audio_delivery = "url"
    logger.info(
        "tts_ready cache=%s hit=%s delivery=%s chars=%d inline_bytes=%d elapsed_ms=%.1f",
        result.cache_key,
        result.cache_hit,
        response.tts.audio_delivery,
        len(tts_text),
        response.tts.audio_bytes,
        (perf_counter() - started) * 1000,
    )
    return response


def _requested_audio_delivery(request: ChatRequest) -> str:
    """读取本次请求希望的音频传输方式，并兼容旧的 tts_inline_audio 布尔值。"""
    delivery = str(getattr(request, "tts_audio_delivery", "inline") or "inline").strip().lower()
    if not getattr(request, "tts_inline_audio", True):
        delivery = "url"
    return delivery if delivery in {"inline", "url", "auto"} else "inline"


async def _attach_inline_audio_if_selected(
    request: ChatRequest,
    response: ChatResponse,
    audio_path: str,
) -> bool:
    """把短 WAV 直接放入响应，减少 Godot 的第二次 HTTP 请求。

    这是服务端选择的传输策略：成功时 ``audio_delivery=inline``；不适合内联
    时由服务端明确改选 ``audio_delivery=url``，Godot 端不自行猜测。
    """
    if request.tts_inline_max_bytes <= 0:
        return False
    path = Path(audio_path)
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= 44 or size > request.tts_inline_max_bytes:
        return False
    try:
        audio = await asyncio.to_thread(path.read_bytes)
    except OSError:
        return False
    response.tts.audio_base64 = base64.b64encode(audio).decode("ascii")
    response.tts.audio_bytes = len(audio)
    return True

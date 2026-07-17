from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from time import perf_counter

from ..schemas import ChatRequest, ChatResponse, DialogueSegment, TTSOutput
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

    # 首选新协议：每个 dialogue_segment 单独合成一段短音频。
    # 这能让 Godot 端“一句字幕 + 一句语音”顺序播放，避免整段对白的 WAV
    # 生成/传输/解码完成后才开始发声。
    if response.dialogue_segments:
        return await _attach_segment_tts(service, request, response, profile)

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
    elif await _attach_inline_audio_if_selected(request, response.tts, result.path):
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


async def _attach_segment_tts(
    service: TTSService,
    request: ChatRequest,
    response: ChatResponse,
    profile: str,
) -> ChatResponse:
    """给每个对白段落独立生成 TTS，并用顶层 tts 保留整体状态。"""
    response.tts = TTSOutput(requested=True, voice_profile=profile)
    first_generated: TTSOutput | None = None
    generated_count = 0
    started_all = perf_counter()
    for index, segment in enumerate(response.dialogue_segments):
        segment.tts = TTSOutput(requested=True, voice_profile=profile)
        if not segment.text.strip():
            segment.tts.error = "segment_text_empty"
            continue
        try:
            tts_text, text_source = _segment_tts_text(request, segment, index)
            started = perf_counter()
            result = await service.synthesize(
                TTSSynthesisRequest(
                    text=tts_text,
                    voice_profile=profile,
                    emotion=segment.emotion or response.emotion,
                    emotion_intensity=response.emotion_intensity,
                    speaker_id=request.tts_speaker_id,
                )
            )
            segment.tts.generated = True
            segment.tts.provider = service.settings.provider
            segment.tts.text_source = text_source
            segment.tts.audio_url = f"/tts/audio/{result.cache_key}"
            segment.tts.cache_key = result.cache_key
            segment.tts.cache_hit = result.cache_hit
            requested_delivery = _requested_audio_delivery(request)
            if requested_delivery == "url":
                segment.tts.audio_delivery = "url"
            elif await _attach_inline_audio_if_selected(request, segment.tts, result.path):
                segment.tts.audio_delivery = "inline"
            else:
                segment.tts.audio_delivery = "url"
            generated_count += 1
            if first_generated is None:
                first_generated = segment.tts.model_copy(deep=True)
            logger.info(
                "tts_segment_ready index=%d cache=%s hit=%s delivery=%s chars=%d inline_bytes=%d elapsed_ms=%.1f",
                index,
                result.cache_key,
                result.cache_hit,
                segment.tts.audio_delivery,
                len(tts_text),
                segment.tts.audio_bytes,
                (perf_counter() - started) * 1000,
            )
        except VoicevoxError as exc:
            logger.warning("tts_segment_failed index=%d provider=voicevox error=%s", index, exc)
            segment.tts.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 单段 TTS 失败不能阻断文字响应
            logger.exception("tts_segment_failed_unexpected index=%d", index)
            segment.tts.error = f"{exc.__class__.__name__}: {exc}"

    if first_generated is not None:
        response.tts = first_generated
        response.tts.requested = True
    else:
        response.tts.generated = False
        response.tts.error = "all_segments_tts_failed"
    logger.info(
        "tts_segments_done generated=%d total=%d elapsed_ms=%.1f",
        generated_count,
        len(response.dialogue_segments),
        (perf_counter() - started_all) * 1000,
    )
    return response


def _segment_tts_text(request: ChatRequest, segment: DialogueSegment, index: int) -> tuple[str, str]:
    """选择某个 segment 的 TTS 文本；日语字段可选且按段对应。"""
    if request.generate_japanese and segment.text_ja.strip():
        return segment.text_ja, f"dialogue_segments[{index}].text_ja"
    return segment.text, f"dialogue_segments[{index}].text"


def _requested_audio_delivery(request: ChatRequest) -> str:
    """读取本次请求希望的音频传输方式，并兼容旧的 tts_inline_audio 布尔值。"""
    delivery = str(getattr(request, "tts_audio_delivery", "inline") or "inline").strip().lower()
    if not getattr(request, "tts_inline_audio", True):
        delivery = "url"
    return delivery if delivery in {"inline", "url", "auto"} else "inline"


async def _attach_inline_audio_if_selected(
    request: ChatRequest,
    tts: TTSOutput,
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
    tts.audio_base64 = base64.b64encode(audio).decode("ascii")
    tts.audio_bytes = len(audio)
    return True

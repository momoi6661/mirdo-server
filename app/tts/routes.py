from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .config import TTSSettings
from .dialogue import load_dialogue
from .models import TTSHealthResponse, TTSInfoResponse, TTSSynthesisRequest
from .service import TTSService
from .voicevox import VoicevoxError


router = APIRouter(prefix="/tts", tags=["tts"])


def _service(request: Request) -> TTSService:
    """从当前 FastAPI 应用取出常驻 TTS Service。"""

    service = getattr(request.app.state, "tts_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="TTS service is disabled")
    return service


def _settings(request: Request) -> TTSSettings:
    settings = getattr(request.app.state, "tts_settings", None)
    if settings is None:
        raise HTTPException(status_code=503, detail="TTS settings are not initialized")
    return settings


@router.get("/health", response_model=TTSHealthResponse)
async def health(request: Request) -> TTSHealthResponse:
    """检查后端是否能访问 VOICEVOX，不会在主健康检查里自动调用它。"""

    settings = _settings(request)
    service = _service(request)
    try:
        version = await service.health()
        return TTSHealthResponse(
            ok=True,
            provider=settings.provider,
            engine_url=settings.engine_url,
            version=version,
        )
    except VoicevoxError as exc:
        return TTSHealthResponse(
            ok=False,
            provider=settings.provider,
            engine_url=settings.engine_url,
            message=str(exc),
        )


@router.get("/info", response_model=TTSInfoResponse)
async def info(request: Request) -> TTSInfoResponse:
    """返回当前角色和缓存配置，方便先用 HTTP 调试而不是直接改 Godot。"""

    settings = _settings(request)
    service = _service(request)
    return TTSInfoResponse(
        enabled=settings.enabled,
        provider=settings.provider,
        engine_url=settings.engine_url,
        default_speaker_id=service.default_speaker_id,
        cache_enabled=settings.cache_enabled,
        cache_dir=str(settings.cache_dir),
        profiles=service.profile_summaries(),
    )


@router.get("/speakers")
async def speakers(request: Request) -> dict[str, object]:
    """读取 VOICEVOX 当前安装的所有音色，确认 speaker_id 是否存在。"""

    try:
        return {"ok": True, "speakers": await _service(request).speakers()}
    except VoicevoxError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/audio/{cache_key}")
async def audio(request: Request, cache_key: str) -> Response:
    """按聊天响应里的 cache_key 返回已经生成的 WAV。"""

    path = _service(request).cached_audio(cache_key)
    if path is None:
        raise HTTPException(status_code=404, detail="audio cache not found")
    # 音频是按内容哈希生成的不可变文件；允许 Godot/系统缓存，重复播放时不再
    # 重新读取网络内容。Godot 自己仍保留解码后的内存缓存作为第一优先级。
    return await _wav_bytes_response(
        path,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-TTS-Cache-Key": cache_key,
        },
    )


@router.get("/dialogue/{locale}/{character_id}/{scene}")
async def dialogue(request: Request, locale: str, character_id: str, scene: str) -> dict[str, object]:
    """读取语言台词文件；Godot 接入前可用此接口检查目录和命名。"""

    settings = _settings(request)
    try:
        document = load_dialogue(
            settings.dialogue_dir,
            locale=locale,
            character_id=character_id,
            scene=scene,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"dialogue not found: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "dialogue": document.model_dump(mode="json")}


@router.post("/synthesize")
async def synthesize(request: Request, payload: TTSSynthesisRequest) -> Response:
    """按需生成 WAV；适合不想让聊天接口承担语音生成延迟的客户端。"""

    settings = _settings(request)
    try:
        result = await _service(request).synthesize(payload)
    except VoicevoxError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return await _wav_bytes_response(
        Path(result.path),
        headers={
            "X-TTS-Provider": settings.provider,
            "X-TTS-Profile": result.profile_id,
            "X-TTS-Cache": "hit" if result.cache_hit else "miss",
            "X-TTS-Cache-Key": result.cache_key,
        },
    )


async def _wav_bytes_response(path: Path, *, headers: dict[str, str]) -> Response:
    """快速返回已缓存 WAV。

    ``FileResponse`` 适合大文件和断点下载，但 Docker Desktop + Windows bind
    mount 下发送几百 KB 的 TTS WAV 会有明显额外延迟。TTS 文件本来就是短对白，
    直接在线程里读成 bytes 再交给 Starlette Response，通常比二次文件发送快。
    """
    body = await asyncio.to_thread(path.read_bytes)
    final_headers = dict(headers)
    final_headers["Content-Length"] = str(len(body))
    return Response(content=body, media_type="audio/wav", headers=final_headers)

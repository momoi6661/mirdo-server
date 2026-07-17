from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from contextlib import suppress

from .config import TTSSettings
from .models import TTSResult, TTSSynthesisRequest
from .profiles import DEFAULT_PROFILE_ID, load_voice_profiles
from .voicevox import VoicevoxClient, VoicevoxError


class TTSService:
    """独立的 TTS 业务层：缓存、串行生成和 Provider 生命周期。"""

    def __init__(self, settings: TTSSettings) -> None:
        if settings.provider.lower() != "voicevox":
            raise ValueError(f"当前测试服务只支持 voicevox，收到: {settings.provider}")
        self.settings = settings
        self.voicevox = VoicevoxClient(
            engine_url=settings.engine_url,
            timeout=settings.request_timeout,
        )
        self.profiles = load_voice_profiles(settings.profile_dir)
        self._generation_lock = asyncio.Lock()
        # cache_key -> 后台合成任务。/chat 可以先返回第一句，后续句子由这些任务继续生成。
        self._inflight: dict[str, asyncio.Task[TTSResult]] = {}

    async def synthesize(self, request: TTSSynthesisRequest) -> TTSResult:
        """生成 WAV；同样文本和参数优先返回缓存，不重复请求引擎。"""
        effective_request, speaker_id, cache_key, path, profile_id = self.prepare_request(request)
        if self.settings.cache_enabled and path.is_file() and path.stat().st_size > 44:
            return TTSResult(path=str(path), cache_key=cache_key, cache_hit=True, profile_id=profile_id)

        # 如果同一个 cache_key 已在后台生成，前台请求直接等待同一个任务。
        running = self._inflight.get(cache_key)
        current = asyncio.current_task()
        if running is not None and running is not current and not running.done():
            return await asyncio.shield(running)

        # Engine 通常是单进程模型；串行合成能避免同时请求造成显存和队列抖动。
        async with self._generation_lock:
            if self.settings.cache_enabled and path.is_file() and path.stat().st_size > 44:
                return TTSResult(path=str(path), cache_key=cache_key, cache_hit=True, profile_id=profile_id)
            audio = await self.voicevox.query_and_synthesize(effective_request, speaker_id=speaker_id)
            if self.settings.cache_enabled:
                self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
                temp_path = path.with_suffix(".tmp")
                await asyncio.to_thread(temp_path.write_bytes, audio)
                temp_path.replace(path)
                return TTSResult(path=str(path), cache_key=cache_key, cache_hit=False, profile_id=profile_id)

            # 未启用缓存时仍写临时文件，让 HTTP 层可以统一使用 FileResponse。
            temp_path = self.settings.cache_dir / f"{cache_key}.wav"
            self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(temp_path.write_bytes, audio)
            return TTSResult(path=str(temp_path), cache_key=cache_key, cache_hit=False, profile_id=profile_id)

    def prepare_request(self, request: TTSSynthesisRequest) -> tuple[TTSSynthesisRequest, int, str, Path, str]:
        """计算最终音色参数和缓存路径，但不实际调用 VOICEVOX。"""
        profile = self.profiles.get(request.voice_profile, self.profiles[DEFAULT_PROFILE_ID])
        effective_request = profile.apply(request)
        if self.settings.speaker_id is not None:
            effective_request = effective_request.model_copy(update={"speaker_id": self.settings.speaker_id})
        speaker_id = int(effective_request.speaker_id or profile.default_speaker_id)
        cache_key = self._cache_key(effective_request, speaker_id)
        path = self.settings.cache_dir / f"{cache_key}.wav"
        return effective_request, speaker_id, cache_key, path, profile.name

    def queue_synthesis(self, request: TTSSynthesisRequest) -> TTSResult:
        """把音频合成放到后台，立即返回将来可读取的缓存 URL 信息。

        注意：这不是让模型再次运行；只是让 VOICEVOX 在 /chat 返回后继续
        生成后续对白段的 WAV。Godot 播到对应 segment 时再 GET /tts/audio。
        """
        _effective, _speaker_id, cache_key, path, profile_id = self.prepare_request(request)
        cache_hit = self.settings.cache_enabled and path.is_file() and path.stat().st_size > 44
        if not cache_hit and cache_key not in self._inflight:
            task = asyncio.create_task(self.synthesize(request), name=f"tts:{cache_key}")
            self._inflight[cache_key] = task

            def _forget(done: asyncio.Task[TTSResult], key: str = cache_key) -> None:
                self._inflight.pop(key, None)
                with suppress(Exception):
                    done.result()

            task.add_done_callback(_forget)
        return TTSResult(path=str(path), cache_key=cache_key, cache_hit=cache_hit, profile_id=profile_id)

    async def wait_for_cached_audio(self, cache_key: str, *, timeout: float = 20.0) -> Path | None:
        """等待后台合成完成后返回音频路径；给 Godot 后续段 GET 使用。"""
        path = self.cached_audio(cache_key)
        if path is not None:
            return path
        task = self._inflight.get(cache_key)
        if task is not None and not task.done():
            with suppress(asyncio.TimeoutError, VoicevoxError):
                result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
                return Path(result.path)
        return self.cached_audio(cache_key)

    async def health(self) -> str:
        return await self.voicevox.health()

    async def speakers(self) -> list[dict[str, object]]:
        return await self.voicevox.speakers()

    @property
    def default_speaker_id(self) -> int:
        """返回 Mirdo 当前角色文件定义的音色 ID。"""

        return self.profiles[DEFAULT_PROFILE_ID].default_speaker_id

    def profile_summaries(self) -> list[dict[str, object]]:
        """返回角色声线摘要，方便后端联调而不用读取本地文件。"""

        return [profile.summary() for profile in self.profiles.values()]

    def cached_audio(self, cache_key: str) -> Path | None:
        """只允许读取本服务生成的 32 位十六进制缓存文件。"""

        if not re.fullmatch(r"[0-9a-f]{32}", cache_key):
            return None
        path = self.settings.cache_dir / f"{cache_key}.wav"
        return path if path.is_file() and path.stat().st_size > 44 else None

    async def close(self) -> None:
        await self.voicevox.close()

    @staticmethod
    def _cache_key(request: TTSSynthesisRequest, speaker_id: int) -> str:
        """把所有影响声音的参数加入哈希，避免复用错误音频。"""
        payload = request.model_dump(mode="json", exclude_none=True)
        payload["speaker_id"] = speaker_id
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:32]


__all__ = ["TTSService", "VoicevoxError"]

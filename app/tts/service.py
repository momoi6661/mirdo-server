from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

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

    async def synthesize(self, request: TTSSynthesisRequest) -> TTSResult:
        """生成 WAV；同样文本和参数优先返回缓存，不重复请求引擎。"""
        profile = self.profiles.get(request.voice_profile, self.profiles[DEFAULT_PROFILE_ID])
        effective_request = profile.apply(request)
        if self.settings.speaker_id is not None:
            effective_request = effective_request.model_copy(update={"speaker_id": self.settings.speaker_id})
        speaker_id = int(effective_request.speaker_id or profile.default_speaker_id)
        cache_key = self._cache_key(effective_request, speaker_id)
        path = self.settings.cache_dir / f"{cache_key}.wav"
        if self.settings.cache_enabled and path.is_file() and path.stat().st_size > 44:
            return TTSResult(path=str(path), cache_key=cache_key, cache_hit=True, profile_id=profile.name)

        # Engine 通常是单进程模型；串行合成能避免同时请求造成显存和队列抖动。
        async with self._generation_lock:
            if self.settings.cache_enabled and path.is_file() and path.stat().st_size > 44:
                return TTSResult(path=str(path), cache_key=cache_key, cache_hit=True, profile_id=profile.name)
            audio = await self.voicevox.query_and_synthesize(effective_request, speaker_id=speaker_id)
            if self.settings.cache_enabled:
                self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
                temp_path = path.with_suffix(".tmp")
                await asyncio.to_thread(temp_path.write_bytes, audio)
                temp_path.replace(path)
                return TTSResult(path=str(path), cache_key=cache_key, cache_hit=False, profile_id=profile.name)

            # 未启用缓存时仍写临时文件，让 HTTP 层可以统一使用 FileResponse。
            temp_path = self.settings.cache_dir / f"{cache_key}.wav"
            self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(temp_path.write_bytes, audio)
            return TTSResult(path=str(temp_path), cache_key=cache_key, cache_hit=False, profile_id=profile.name)

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

from __future__ import annotations

from typing import Any

import httpx

from .models import TTSSynthesisRequest


class VoicevoxError(RuntimeError):
    """VOICEVOX 引擎不可用或返回了无效响应。"""


class VoicevoxClient:
    """VOICEVOX Engine 的最小异步适配器。

    ``AsyncClient`` 在整个 TTS 服务生命周期内复用，避免每次生成重新建立
    TCP 连接。模型进程仍由 VOICEVOX Engine 自己常驻管理。
    """

    def __init__(self, *, engine_url: str, timeout: float) -> None:
        self.engine_url = engine_url.rstrip("/")
        # 本地引擎不应该经过系统代理；连接失败时只等很短时间，不能拖慢 Chat。
        request_timeout = httpx.Timeout(timeout, connect=min(timeout, 0.75))
        self._client = httpx.AsyncClient(
            base_url=self.engine_url,
            timeout=request_timeout,
            trust_env=False,
            headers={"Accept": "application/json"},
        )

    async def query_and_synthesize(self, request: TTSSynthesisRequest, *, speaker_id: int) -> bytes:
        """调用标准的 audio_query → synthesis 两阶段接口。"""
        try:
            query_response = await self._client.post(
                "/audio_query",
                params={"text": request.text, "speaker": speaker_id},
            )
            query_response.raise_for_status()
            query = query_response.json()
            if not isinstance(query, dict):
                raise VoicevoxError("audio_query 返回的 JSON 不是对象")

            # VOICEVOX 默认值保留给引擎；只有调用方明确传入的值才覆盖。
            overrides: dict[str, float | None] = {
                "speedScale": request.speed_scale,
                "pitchScale": request.pitch_scale,
                "intonationScale": request.intonation_scale,
                "volumeScale": request.volume_scale,
                "prePhonemeLength": request.pre_phoneme_length,
                "postPhonemeLength": request.post_phoneme_length,
            }
            for key, value in overrides.items():
                if value is not None:
                    query[key] = value

            audio_response = await self._client.post(
                "/synthesis",
                params={"speaker": speaker_id},
                json=query,
                headers={"Content-Type": "application/json"},
            )
            audio_response.raise_for_status()
            if not audio_response.content:
                raise VoicevoxError("synthesis 返回了空音频")
            return audio_response.content
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:240]
            raise VoicevoxError(f"VOICEVOX HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise VoicevoxError(f"无法连接 VOICEVOX: {exc}") from exc

    async def health(self) -> str:
        """读取版本号，供测试服务健康检查使用。"""
        try:
            response = await self._client.get("/version")
            response.raise_for_status()
            return response.text.strip().strip('"')
        except httpx.HTTPError as exc:
            raise VoicevoxError(f"VOICEVOX 健康检查失败: {exc}") from exc

    async def speakers(self) -> list[dict[str, Any]]:
        """读取可用音色目录，方便测试时选择 speaker_id。"""
        try:
            response = await self._client.get("/speakers")
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, list) else []
        except httpx.HTTPError as exc:
            raise VoicevoxError(f"读取 VOICEVOX 音色失败: {exc}") from exc

    async def close(self) -> None:
        await self._client.aclose()

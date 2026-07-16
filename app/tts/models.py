from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TTSSynthesisRequest(BaseModel):
    """VOICEVOX 无关的文本生成请求。

    请求只暴露常用的语音控制参数；VOICEVOX 的 ``audio_query`` 细节留在
    Provider 内部，未来替换 IndexTTS2 时不会污染上层接口。
    """

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=1200)
    voice_profile: str = "mirdo_ja"
    emotion: str = "平静"
    emotion_intensity: float = Field(default=0.65, ge=0.0, le=1.0)
    speaker_id: int | None = Field(default=None, ge=0)
    speed_scale: float | None = Field(default=None, ge=0.5, le=2.0)
    pitch_scale: float | None = Field(default=None, ge=-0.15, le=0.15)
    intonation_scale: float | None = Field(default=None, ge=0.0, le=2.0)
    volume_scale: float | None = Field(default=None, ge=0.0, le=2.0)
    pre_phoneme_length: float | None = Field(default=None, ge=0.0, le=2.0)
    post_phoneme_length: float | None = Field(default=None, ge=0.0, le=2.0)

    @field_validator("text", "voice_profile", "emotion", mode="before")
    @classmethod
    def _trim_text(cls, value: object) -> str:
        return str(value or "").strip()


class TTSHealthResponse(BaseModel):
    ok: bool
    provider: str
    engine_url: str
    version: str = ""
    message: str = ""


class TTSInfoResponse(BaseModel):
    provider: str
    engine_url: str
    default_speaker_id: int
    cache_enabled: bool
    cache_dir: str
    enabled: bool = True
    profiles: list[dict[str, object]] = Field(default_factory=list)


class TTSResult:
    """服务内部返回的音频文件信息。"""

    def __init__(self, *, path: str, cache_key: str, cache_hit: bool, profile_id: str = "") -> None:
        self.path = path
        self.cache_key = cache_key
        self.cache_hit = cache_hit
        self.profile_id = profile_id

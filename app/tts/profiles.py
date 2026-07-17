from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .models import TTSSynthesisRequest

DEFAULT_PROFILE_ID = "mirdo_ja"


def default_emotion_presets() -> dict[str, "VoiceParameters"]:
    """返回所有声线共用的一套基础情绪参数。

    角色 JSON 只需要声明 ``speaker_id``；如果没有特殊调音，就使用这里的
    安全默认值，避免每个音色文件重复几十行参数。
    """

    return {
        "平静": VoiceParameters(speed_scale=0.95, pitch_scale=0.015, intonation_scale=1.02, volume_scale=1.00, pre_phoneme_length=0.08, post_phoneme_length=0.10),
        "温柔": VoiceParameters(speed_scale=0.92, pitch_scale=0.025, intonation_scale=0.94, volume_scale=0.98, pre_phoneme_length=0.10, post_phoneme_length=0.12),
        "开心": VoiceParameters(speed_scale=1.03, pitch_scale=0.045, intonation_scale=1.12, volume_scale=1.02, pre_phoneme_length=0.06, post_phoneme_length=0.08),
        "害羞": VoiceParameters(speed_scale=0.90, pitch_scale=0.035, intonation_scale=0.88, volume_scale=0.96, pre_phoneme_length=0.12, post_phoneme_length=0.14),
        "惊讶": VoiceParameters(speed_scale=1.06, pitch_scale=0.075, intonation_scale=1.22, volume_scale=1.03, pre_phoneme_length=0.04, post_phoneme_length=0.06),
        "担心": VoiceParameters(speed_scale=0.96, pitch_scale=-0.015, intonation_scale=1.08, volume_scale=0.98, pre_phoneme_length=0.09, post_phoneme_length=0.11),
        "疲惫": VoiceParameters(speed_scale=0.84, pitch_scale=-0.035, intonation_scale=0.78, volume_scale=0.92, pre_phoneme_length=0.14, post_phoneme_length=0.16),
        "生气": VoiceParameters(speed_scale=1.02, pitch_scale=0.025, intonation_scale=1.18, volume_scale=1.04, pre_phoneme_length=0.04, post_phoneme_length=0.06),
    }


def default_emotion_aliases() -> dict[str, str]:
    """把常见中文/英文情绪名称统一到基础情绪。"""

    return {
        "平和": "平静",
        "neutral": "平静",
        "calm": "平静",
        "soft": "温柔",
        "安心": "温柔",
        "放松": "温柔",
        "happy": "开心",
        "joy": "开心",
        "期待": "开心",
        "shy": "害羞",
        "撒娇": "害羞",
        "surprised": "惊讶",
        "疑惑": "惊讶",
        "困惑": "惊讶",
        "worried": "担心",
        "紧张": "担心",
        "害怕": "担心",
        "sad": "疲惫",
        "tired": "疲惫",
        "难过": "疲惫",
        "委屈": "疲惫",
        "angry": "生气",
    }


class VoiceParameters(BaseModel):
    """VOICEVOX 的可控参数；范围由模型校验，Agent 不能随意破坏声线。"""

    speed_scale: float = Field(ge=0.5, le=2.0)
    pitch_scale: float = Field(ge=-0.15, le=0.15)
    intonation_scale: float = Field(ge=0.0, le=2.0)
    volume_scale: float = Field(ge=0.0, le=2.0)
    pre_phoneme_length: float = Field(ge=0.0, le=2.0)
    post_phoneme_length: float = Field(ge=0.0, le=2.0)


class CharacterVoiceProfile(BaseModel):
    """角色声线定义，对应 ``data/tts/characters/*.json``。"""

    model_config = ConfigDict(extra="ignore")

    # 允许 ``mirdo_ja`` 以及 ``mirdo_ja_jp``，与文件名命名规则一致。
    profile_id: str = Field(pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
    character_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    locale: str = Field(min_length=2)
    dialogue_locale: str = Field(min_length=2)
    provider: str = "voicevox"
    speaker_id: int = Field(ge=0)
    default_emotion: str = "平静"
    emotion_presets: dict[str, VoiceParameters] = Field(default_factory=default_emotion_presets)
    aliases: dict[str, str] = Field(default_factory=default_emotion_aliases)

    def normalize_emotion(self, emotion: str) -> str:
        """把中英文情绪名称归一到角色文件里定义的有限词表。"""

        value = str(emotion or "").strip()
        alias = self.aliases.get(value.lower(), value)
        return alias if alias in self.emotion_presets else self.default_emotion

    def parameters(self, emotion: str, intensity: float) -> VoiceParameters:
        """在平静基线和目标情绪之间平滑插值，避免台词之间突然变声。"""

        normalized = self.normalize_emotion(emotion)
        base = self.emotion_presets[self.default_emotion]
        target = self.emotion_presets[normalized]
        amount = max(0.0, min(float(intensity), 1.0))
        return VoiceParameters(
            speed_scale=_mix(base.speed_scale, target.speed_scale, amount),
            pitch_scale=_mix(base.pitch_scale, target.pitch_scale, amount),
            intonation_scale=_mix(base.intonation_scale, target.intonation_scale, amount),
            volume_scale=_mix(base.volume_scale, target.volume_scale, amount),
            pre_phoneme_length=_mix(base.pre_phoneme_length, target.pre_phoneme_length, amount),
            post_phoneme_length=_mix(base.post_phoneme_length, target.post_phoneme_length, amount),
        )

    def apply(self, request: TTSSynthesisRequest) -> TTSSynthesisRequest:
        """只补齐请求中没有指定的参数，调试时仍可手动覆盖单个参数。"""

        params = self.parameters(request.emotion, request.emotion_intensity)
        update = {
            "speaker_id": request.speaker_id if request.speaker_id is not None else self.speaker_id,
            "speed_scale": request.speed_scale if request.speed_scale is not None else params.speed_scale,
            "pitch_scale": request.pitch_scale if request.pitch_scale is not None else params.pitch_scale,
            "intonation_scale": request.intonation_scale if request.intonation_scale is not None else params.intonation_scale,
            "volume_scale": request.volume_scale if request.volume_scale is not None else params.volume_scale,
            "pre_phoneme_length": request.pre_phoneme_length if request.pre_phoneme_length is not None else params.pre_phoneme_length,
            "post_phoneme_length": request.post_phoneme_length if request.post_phoneme_length is not None else params.post_phoneme_length,
        }
        return request.model_copy(update=update)

    def summary(self) -> dict[str, object]:
        """给调试接口返回可读信息，不暴露内部实现细节。"""

        return {
            "profile_id": self.profile_id,
            "character_id": self.character_id,
            "display_name": self.display_name,
            "locale": self.locale,
            "dialogue_locale": self.dialogue_locale,
            "provider": self.provider,
            "speaker_id": self.speaker_id,
            "emotions": sorted(self.emotion_presets),
        }


class MirdoVoiceProfile:
    """保持旧调用方式的 Mirdo 声线包装器。

    业务代码只需要 ``MirdoVoiceProfile().apply(request)``；实际数字参数来自
    JSON 角色文件，便于非 Python 使用者调音。
    """

    def __init__(self, definition: CharacterVoiceProfile | None = None) -> None:
        self.definition = definition or builtin_mirdo_profile()

    @property
    def name(self) -> str:
        """返回角色文件自己的 profile_id，支持未来增加其他角色。"""

        return self.definition.profile_id

    @property
    def default_speaker_id(self) -> int:
        return self.definition.speaker_id

    def normalize_emotion(self, emotion: str) -> str:
        return self.definition.normalize_emotion(emotion)

    def parameters(self, emotion: str, intensity: float = 0.65) -> VoiceParameters:
        return self.definition.parameters(emotion, intensity)

    def apply(self, request: TTSSynthesisRequest) -> TTSSynthesisRequest:
        return self.definition.apply(request)

    def summary(self) -> dict[str, object]:
        return self.definition.summary()


def load_voice_profiles(directory: Path) -> dict[str, MirdoVoiceProfile]:
    """加载角色文件；缺少 Mirdo 文件时仍提供安全的内置默认值。"""

    profiles: dict[str, MirdoVoiceProfile] = {}
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            definition = CharacterVoiceProfile.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
            profiles[definition.profile_id] = MirdoVoiceProfile(definition)
    profiles.setdefault(DEFAULT_PROFILE_ID, MirdoVoiceProfile())
    return profiles


def builtin_mirdo_profile() -> CharacterVoiceProfile:
    """没有外部文件时的兜底配置，默认使用もち子さん 20。"""
    return CharacterVoiceProfile(
        profile_id="mirdo_ja",
        character_id="mirdo",
        display_name="Mirdo",
        locale="ja-JP",
        dialogue_locale="ja_jp",
        speaker_id=20,
    )


def _mix(base: float, target: float, amount: float) -> float:
    """在线性范围内混合参数。"""

    return round(base + (target - base) * amount, 4)


__all__ = [
    "CharacterVoiceProfile",
    "MirdoVoiceProfile",
    "VoiceParameters",
    "load_voice_profiles",
]

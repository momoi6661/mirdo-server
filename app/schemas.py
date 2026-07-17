from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# existing imports/classes are below intentionally rewritten as one file
from pydantic import field_validator, model_validator


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    proxy_url: str = ""

    @field_validator("base_url", "api_key", "model", "proxy_url", mode="before")
    @classmethod
    def _stringify_and_trim(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slashes(cls, value: str) -> str:
        while len(value) > 1 and value.endswith("/"):
            value = value[:-1]
        return value

    @property
    def is_complete(self) -> bool:
        return bool(self.base_url and self.model)


class NpcStats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hunger: float = 0.0
    thirst: float = 0.0
    mood: float = 0.0
    favor: float = 0.0


class SteeringInput(BaseModel):
    """客户端对正在生成或呈现中的回合发出的最新引导。

    已经提交给上游模型的 HTTP 请求无法原地修改，因此实现方式和 Codex
    类似：旧请求失效，新请求携带目标 request id 和当前阶段重新进入 Agent。
    """

    model_config = ConfigDict(extra="ignore")

    mode: Literal["none", "interrupt", "append", "replace"] = "none"
    phase: Literal["idle", "generation", "presentation", "action"] = "idle"
    target_request_id: str = ""
    target_client_sequence: int = Field(default=0, ge=0)
    interrupted_dialogue: str = Field(default="", max_length=500)
    # presentation 边界介入时，Godot 会告诉后端：当前已经自然说完了哪一句。
    heard_dialogue: str = Field(default="", max_length=500)
    # segment_finished / speech_finished / segment_failed 等，不伪装成玩家文本。
    boundary_reason: str = ""
    reason: str = ""

    @field_validator("target_request_id", "interrupted_dialogue", "heard_dialogue", "boundary_reason", "reason", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = "default_session"
    player_text: str
    day: int = 1
    time: int = 0
    time_min: int | None = None
    npc_stats: NpcStats = Field(default_factory=NpcStats)
    given_item: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    max_context_turns: int = 8
    # 默认只返回文字；请求明确传 true 时，后端才调用 VOICEVOX。
    use_tts: bool = False
    tts_voice_profile: str = "mirdo_ja"
    # 可选的 VOICEVOX 风格 ID；传入后优先于 profile 文件里的 speaker_id。
    tts_speaker_id: int | None = Field(default=None, ge=0)
    # 音频传输策略：inline=随 /chat JSON 返回；url=只返回可下载 URL；auto=由后端按大小选择。
    tts_audio_delivery: str = "inline"
    # 兼容旧 Godot 字段；False 等价于 tts_audio_delivery=url。
    tts_inline_audio: bool = True
    tts_inline_max_bytes: int = Field(default=786_432, ge=0, le=2_000_000)
    # 默认只生成中文对白；请求明确传 true 时，Agent 才补充平行的日语字段。
    generate_japanese: bool = False
    provider: ProviderConfig | None = None
    # Godot 用它们标记“同一输入框里的最新意图”。旧请求完成后若发现自己
    # 已被更新，会被服务端标记为 superseded，不再写入助手回合。
    client_request_id: str = ""
    client_sequence: int = Field(default=0, ge=0)
    supersedes_request_id: str = ""
    # 新输入若针对正在生成/播放的旧回合，使用结构化 steering 元数据，
    # 不再把“玩家刚刚改口……”之类的系统说明混进玩家原话。
    steering: SteeringInput = Field(default_factory=SteeringInput)

    @field_validator("session_id", mode="before")
    @classmethod
    def _clean_session_id(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        return text or "default_session"

    @field_validator("player_text", mode="before")
    @classmethod
    def _clean_player_text(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValueError("player_text must not be empty")
        return text

    @field_validator("given_item", mode="before")
    @classmethod
    def _clean_given_item(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("tts_audio_delivery", mode="before")
    @classmethod
    def _clean_tts_audio_delivery(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip().lower()
        return text if text in {"inline", "url", "auto"} else "inline"

    @field_validator("client_request_id", "supersedes_request_id", mode="before")
    @classmethod
    def _clean_client_request_id(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @model_validator(mode="after")
    def _normalize_limits(self) -> "ChatRequest":
        self.max_context_turns = max(0, min(int(self.max_context_turns), 50))
        return self

    def effective_time_min(self) -> int:
        if self.time_min is not None:
            return int(self.time_min)
        return int(self.time)


class GodotActionResultRequest(BaseModel):
    """Godot 作为 Agent 工具执行器回传的一次动作结果。

    这不是事件推送，也不是伪造的玩家消息。Godot 完成当前动作后只发起
    一次请求，Server 将结果作为 tool result 交给 Mirdo Agent，再返回下一
    个对白或动作步骤。这样后端不会在没有新事实时自行猜测世界状态。
    """

    model_config = ConfigDict(extra="allow")

    session_id: str = "default_session"
    tool_call_id: str = ""
    task_id: str = ""
    chain_id: str = ""
    step_id: str = ""
    command: str = ""
    target_ref: str = ""
    event: str = "navigation_goal_finished"
    status: str = "succeeded"
    ok: bool = True
    action_result: dict[str, Any] = Field(default_factory=dict)
    observation: dict[str, Any] = Field(default_factory=dict)
    source_decision: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    npc_stats: NpcStats = Field(default_factory=NpcStats)
    day: int = 1
    time: int = 0
    time_min: int | None = None
    given_item: str = ""
    # 动作结果也只在请求明确要求时生成语音，避免后台无意触发引擎。
    use_tts: bool = False
    tts_voice_profile: str = "mirdo_ja"
    # Godot 动作结果回合也可以临时切换音色，不需要修改存档配置。
    tts_speaker_id: int | None = Field(default=None, ge=0)
    tts_audio_delivery: str = "inline"
    tts_inline_audio: bool = True
    tts_inline_max_bytes: int = Field(default=786_432, ge=0, le=2_000_000)
    generate_japanese: bool = False
    provider: ProviderConfig | None = None
    client_request_id: str = ""
    client_sequence: int = Field(default=0, ge=0)
    supersedes_request_id: str = ""

    @field_validator(
        "session_id",
        "tool_call_id",
        "task_id",
        "chain_id",
        "step_id",
        "command",
        "target_ref",
        "event",
        "status",
        "given_item",
        mode="before",
    )
    @classmethod
    def _clean_protocol_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("session_id")
    @classmethod
    def _default_protocol_session(cls, value: str) -> str:
        return value or "default_session"

    @field_validator("tts_audio_delivery", mode="before")
    @classmethod
    def _clean_protocol_audio_delivery(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip().lower()
        return text if text in {"inline", "url", "auto"} else "inline"

    @field_validator("action_result", "observation", "source_decision", "context", mode="before")
    @classmethod
    def _clean_protocol_dict(cls, value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @model_validator(mode="after")
    def _normalize_protocol_result(self) -> "GodotActionResultRequest":
        self.event = self.event or "navigation_goal_finished"
        self.status = self.status or ("succeeded" if self.ok else "failed")
        if self.time_min is None:
            self.time_min = int(self.time)
        return self


class MemoryClearRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = "default_session"
    clear_all: bool = False


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clear_first: bool = False
    folder: str = ""


class ExpeditionLoadoutItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item_id: str = ""
    name: str = ""
    category: str = ""
    amount: int = Field(default=1, ge=1, le=99)
    tags: list[str] = Field(default_factory=list)
    ai_rule_hint: str = ""


class ExpeditionLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    name: str = ""
    description: str = ""
    route_hint: str = ""
    threat_level: int = Field(default=1, ge=0, le=5)
    loot_bias_tags: list[str] = Field(default_factory=list)
    recommended_tools: list[str] = Field(default_factory=list)
    detail_notes: list[str] = Field(default_factory=list)
    ai_exploration_rule: str = ""
    discoverable: bool = False


class ExpeditionTimeInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    route_minutes: int = Field(default=0, ge=0, le=1440)
    search_minutes: int = Field(default=0, ge=0, le=1440)
    total_minutes: int = Field(default=0, ge=0, le=2880)


class ExpeditionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    # 与普通聊天共用默认时间线，这样未显式传 session_id 时，外出也能读到对话记忆。
    session_id: str = "default_session"
    location: ExpeditionLocation
    loadout: list[ExpeditionLoadoutItem] = Field(default_factory=list)
    time: ExpeditionTimeInfo = Field(default_factory=ExpeditionTimeInfo)
    available_loot: dict[str, list[str]] = Field(default_factory=dict)
    unlocked_neighbors: list[str] = Field(default_factory=list)
    # 外出是主角的行动；这里允许 Godot 传入额外的世界状态，但不会把它当成 Mirdo 对话。
    context: dict[str, Any] = Field(default_factory=dict)
    provider: ProviderConfig | None = None

    @field_validator("session_id", mode="before")
    @classmethod
    def _clean_expedition_session_id(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        return text or "default_session"


class ExpeditionLootEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item_path: str = ""
    item_name: str = ""
    amount: int = Field(default=1, ge=1, le=99)
    tag: str = "物资"


class ExpeditionStoryMarker(BaseModel):
    """一次外出留下的可追踪剧情标记。

    ``continuity_key`` 用来把同一地点或同一线索串起来；``status`` 让 GM 知道
    下次应该继续哪个未完事项，而不是重新随机一个完全不同的故事。
    """

    model_config = ConfigDict(extra="ignore")

    continuity_key: str = ""
    kind: str = "discovery"
    summary: str = ""
    location_id: str = ""
    status: str = "active"
    tags: list[str] = Field(default_factory=list)
    next_hooks: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.6, ge=0.0, le=1.0)


class ExpeditionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool = True
    session_id: str = "default_session"
    turn_id: int = 0
    forked_from: str = ""
    forked_at_turn_id: int = 0
    title: str = "外出行动报告"
    summary: str = ""
    story: str = ""
    experience: list[str] = Field(default_factory=list)
    risk_result: str = ""
    loot: list[ExpeditionLootEntry] = Field(default_factory=list)
    discovered_clues: list[str] = Field(default_factory=list)
    # AI 对“这次故事以后怎么接着讲”的显式判断，供下一次 GM 回合使用。
    search_focus: list[str] = Field(default_factory=list)
    story_markers: list[ExpeditionStoryMarker] = Field(default_factory=list)
    mood: str = "冷静"
    health_damage: float = 0.0
    fallback: bool = False
    error: str = ""


class ActionStep(BaseModel):
    """动作线中的一个语义步骤。

    Server 只规划步骤之间的因果关系；Godot 每次只执行 ``action_line`` 中的
    ``current_step_id``，完成后把结果连同剩余动作线回传，避免一次响应塞入多个
    同时执行的动作。
    """

    model_config = ConfigDict(extra="ignore")

    step_id: str = ""
    action: str = ""
    command: str = ""
    command_payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    expected_result: str = ""
    success_next_step: str = ""
    failure_next_step: str = ""
    wait_for_result: bool = True
    status: str = "pending"

    @field_validator(
        "step_id",
        "action",
        "command",
        "reason",
        "expected_result",
        "success_next_step",
        "failure_next_step",
        "status",
        mode="before",
    )
    @classmethod
    def _clean_step_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("command_payload", mode="before")
    @classmethod
    def _clean_step_payload(cls, value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}


class TaskControl(BaseModel):
    """Agent 对“当前任务”和新输入之间关系的明确判断。"""

    model_config = ConfigDict(extra="ignore")

    mode: Literal["none", "continue", "pause", "replace", "cancel"] = "none"
    reason: str = ""
    resume_after_reply: bool = True

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_task_control_reason(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()


class TTSOutput(BaseModel):
    """一次聊天是否请求并成功生成了语音。

    ``audio_delivery`` 是本回合唯一应采用的传输方式。Godot 不再自行猜测
    或失败后切换通道；这样音频慢/坏时能从协议字段直接定位。
    """

    requested: bool = False
    generated: bool = False
    provider: str = ""
    voice_profile: str = "mirdo_ja"
    text_source: str = "dialogue"
    audio_delivery: str = "none"
    audio_url: str = ""
    audio_base64: str = ""
    audio_format: str = "wav"
    audio_bytes: int = 0
    cache_key: str = ""
    cache_hit: bool = False
    error: str = ""


class DialogueSegment(BaseModel):
    """一段可独立显示、独立合成 TTS 的 Mirdo 台词。

    这里不是前端分页，而是让 Agent 在生成阶段就把“说话节奏”拆好：
    每段对应一次头顶字幕和一次语音播放。这样 Godot 不需要猜哪里断句，
    也不会把一大段 WAV 全部准备好后才开始呈现。
    """

    model_config = ConfigDict(extra="ignore")

    text: str = ""
    text_ja: str = ""
    emotion: str = ""
    expression: str = ""
    tts: TTSOutput = Field(default_factory=TTSOutput)

    @field_validator("text", "text_ja", "emotion", "expression", mode="before")
    @classmethod
    def _clean_segment_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()


class ChatResponse(BaseModel):
    """Mirdo 本回合的动作线和对白，也是 Godot 的唯一响应契约。

    ``action_line`` 可以包含多个有因果关系的步骤，但只有首个 pending 步骤会
    被 Godot 执行；其余步骤等待真实观察结果后再决定是否继续。
    """

    model_config = ConfigDict(extra="ignore")

    ok: bool = True
    dialogue: str = ""
    # Agent 的中文主对白；只有请求 generate_japanese=true 时才填充此字段。
    dialogue_ja: str = ""
    # 推荐 Agent 填写：1 到 3 个自然短句，每段独立配字幕和 TTS。
    dialogue_segments: list[DialogueSegment] = Field(default_factory=list)
    emotion: str = "平静"
    # 情绪不仅决定表情，也决定 TTS 参数从平静基线向目标情绪插值的幅度。
    emotion_intensity: float = Field(default=0.65, ge=0.0, le=1.0)
    expression: str = ""
    action: str = "Idle"
    action_line: list[ActionStep] = Field(default_factory=list)
    # 类似 Codex 的任务引导：普通插话继续原任务，临时问题暂停后恢复，
    # 新指令替换任务，明确停止则取消任务。
    task_control: TaskControl = Field(default_factory=TaskControl)
    current_step_id: str = ""
    task_id: str = ""
    task_status: str = ""
    task_reason: str = ""
    next_decision_hint: str = ""
    visemes: str = ""
    viseme_sequence: str = ""
    stat_change: NpcStats = Field(default_factory=NpcStats)
    memory_tags: list[str] = Field(default_factory=list)
    session_id: str = "default_session"
    turn_id: int = 0
    forked_from: str = ""
    forked_at_turn_id: int = 0
    used_knowledge: list[dict[str, Any]] = Field(default_factory=list)
    used_memory: list[dict[str, Any]] = Field(default_factory=list)
    used_story_events: list[dict[str, Any]] = Field(default_factory=list)
    memory_updates: list[dict[str, Any]] = Field(default_factory=list)
    story_events: list[dict[str, Any]] = Field(default_factory=list)
    # ``godot_tool_result`` 表示这次回复是对动作工具结果的直接响应。
    response_kind: str = "chat"
    client_request_id: str = ""
    client_sequence: int = 0
    superseded: bool = False
    # 服务端回显本回合实际接收的引导目标，方便 Godot 调试队列归属。
    steering_ack: dict[str, Any] = Field(default_factory=dict)
    tool_call_id: str = ""
    tool_result_ack: dict[str, Any] = Field(default_factory=dict)
    fallback: bool = False
    error: str = ""
    tts: TTSOutput = Field(default_factory=TTSOutput)

    @field_validator("dialogue", "dialogue_ja", "expression", "action", "current_step_id", "task_id", "task_status", "task_reason", "next_decision_hint", "visemes", "viseme_sequence", "session_id", "forked_from", "response_kind", "client_request_id", "tool_call_id", "error", mode="before")
    @classmethod
    def _clean_response_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @model_validator(mode="after")
    def _normalize_dialogue_segments(self) -> "ChatResponse":
        """保持旧字段和新分段字段同步，兼容旧模型/旧 Godot。

        - 新模型可只填 ``dialogue_segments``，这里会拼回 ``dialogue``；
        - 旧模型只填 ``dialogue`` 时，这里会按标点做保底拆句；
        - 顶层 ``dialogue`` 仍作为日志、记忆和旧客户端的兼容字段。
        """
        cleaned: list[DialogueSegment] = []
        for segment in self.dialogue_segments:
            if segment.text.strip():
                cleaned.append(segment)
        if not cleaned and self.dialogue.strip():
            ja_parts = _split_dialogue_text(self.dialogue_ja) if self.dialogue_ja.strip() else []
            for index, text in enumerate(_split_dialogue_text(self.dialogue)):
                cleaned.append(
                    DialogueSegment(
                        text=text,
                        text_ja=ja_parts[index] if index < len(ja_parts) else "",
                        emotion=self.emotion,
                        expression=self.expression,
                    )
                )
        self.dialogue_segments = cleaned[:4]
        if self.dialogue_segments and not self.dialogue.strip():
            self.dialogue = "".join(segment.text for segment in self.dialogue_segments).strip()
        if self.dialogue_segments and not self.dialogue_ja.strip():
            joined_ja = "".join(segment.text_ja for segment in self.dialogue_segments if segment.text_ja.strip()).strip()
            if joined_ja:
                self.dialogue_ja = joined_ja
        return self


def _split_dialogue_text(text: str, *, max_chars: int = 34) -> list[str]:
    """把旧式整段对白保底拆成短句；真正的首选是让 Agent 直接填 segments。"""
    clean = str(text or "").strip()
    if not clean:
        return []
    parts: list[str] = []
    current = ""
    break_chars = set("。！？!?；;，,、：:")
    for ch in clean:
        current += ch
        if ch in break_chars or len(current) >= max_chars:
            piece = current.strip()
            if piece:
                parts.append(piece)
            current = ""
    tail = current.strip()
    if tail:
        parts.append(tail)
    return parts[:4] or [clean]

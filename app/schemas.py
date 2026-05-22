from __future__ import annotations

from typing import Any

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
    provider: ProviderConfig | None = None

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

    @model_validator(mode="after")
    def _normalize_limits(self) -> "ChatRequest":
        self.max_context_turns = max(0, min(int(self.max_context_turns), 50))
        return self

    def effective_time_min(self) -> int:
        if self.time_min is not None:
            return int(self.time_min)
        return int(self.time)


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

    session_id: str = "outing_expedition"
    location: ExpeditionLocation
    loadout: list[ExpeditionLoadoutItem] = Field(default_factory=list)
    time: ExpeditionTimeInfo = Field(default_factory=ExpeditionTimeInfo)
    available_loot: dict[str, list[str]] = Field(default_factory=dict)
    unlocked_neighbors: list[str] = Field(default_factory=list)
    provider: ProviderConfig | None = None

    @field_validator("session_id", mode="before")
    @classmethod
    def _clean_expedition_session_id(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        return text or "outing_expedition"


class ExpeditionLootEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item_path: str = ""
    item_name: str = ""
    amount: int = Field(default=1, ge=1, le=99)
    tag: str = "物资"


class ExpeditionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool = True
    session_id: str = "outing_expedition"
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
    mood: str = "冷静"
    health_damage: float = 0.0
    fallback: bool = False
    error: str = ""


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool = True
    dialogue: str
    emotion: str = "平静"
    expression: str = ""
    action: str = "Idle"
    command: str = ""
    command_payload: dict[str, Any] = Field(default_factory=dict)
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
    memory_updates: list[dict[str, Any]] = Field(default_factory=list)
    fallback: bool = False
    error: str = ""

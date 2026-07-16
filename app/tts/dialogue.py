from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class DialogueLine(BaseModel):
    """一条可直接交给 TTS 的角色台词。"""

    model_config = ConfigDict(extra="ignore")

    line_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    emotion: str = "平静"
    voice_profile: str = "mirdo_ja"


class DialogueDocument(BaseModel):
    """一个语言/场景文件，例如 ``ja_jp/mirdo_opening.json``。"""

    model_config = ConfigDict(extra="ignore")

    character_id: str = Field(min_length=1)
    locale: str = Field(min_length=2)
    scene: str = Field(min_length=1)
    lines: list[DialogueLine] = Field(min_length=1)


def load_dialogue(
    root: Path,
    *,
    locale: str,
    character_id: str,
    scene: str,
) -> DialogueDocument:
    """按固定命名规则读取台词，避免 HTTP 参数拼出任意文件路径。"""

    root = root.resolve()
    filename = f"{character_id}_{scene}.json"
    path = (root / locale / filename).resolve()
    if root not in path.parents:
        raise ValueError("dialogue path escapes configured dialogue directory")
    if not path.is_file():
        raise FileNotFoundError(filename)
    return DialogueDocument.model_validate(json.loads(path.read_text(encoding="utf-8")))

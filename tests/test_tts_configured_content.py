from __future__ import annotations

from pathlib import Path

from app.tts.dialogue import load_dialogue
from app.tts.profiles import load_voice_profiles


SERVER_ROOT = Path(__file__).resolve().parents[1]


def test_mirdo_profile_is_loaded_from_character_file() -> None:
    """角色声线来自 data 文件，而不是散落在 HTTP 路由里。"""

    profiles = load_voice_profiles(SERVER_ROOT / "data/tts/characters")
    mirdo = profiles["mirdo_ja"]
    assert mirdo.default_speaker_id == 20
    assert mirdo.summary()["dialogue_locale"] == "ja_jp"


def test_japanese_dialogue_uses_fixed_file_naming() -> None:
    """语言、角色和场景可以按约定直接定位到台词文件。"""

    document = load_dialogue(
        SERVER_ROOT / "data/dialogue",
        locale="ja_jp",
        character_id="mirdo",
        scene="opening",
    )
    assert document.character_id == "mirdo"
    assert document.locale == "ja_jp"
    assert document.lines[0].voice_profile == "mirdo_ja"

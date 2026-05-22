from pathlib import Path

from app.rag.loaders import KnowledgeLoader
from app.response_parser import ResponseParser


def test_response_parser_preserves_mirdo_expression_and_visemes():
    response = ResponseParser().parse(
        '{"dialogue":"老师，我明白啦。","emotion":"开心","expression":"joy",'
        '"action":"happy_bounce","visemes":"aa、ih、ou、E、oh"}'
    )

    assert response.expression == "joy"
    assert response.action == "happy_bounce"
    assert response.visemes == "aa、ih、ou、E、oh"


def test_mirdo_action_sheet_is_loaded_as_character_actions(tmp_path: Path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "mirdo_action_sheet.md").write_text("# Mirdo\nwork_count_supplies", encoding="utf-8")

    docs = KnowledgeLoader(knowledge_dir).load()

    assert docs
    assert docs[0].metadata["category"] == "character_actions"
    assert docs[0].metadata["source"] == "mirdo_action_sheet.md"


def test_mirdo_action_sheet_contains_rich_original_personality():
    sheet = Path("data/knowledge/mirdo_action_sheet.md").read_text(encoding="utf-8")

    assert "original VRChat-style" in sheet
    assert "gray-brown hair" in sheet
    assert "big headphones" in sheet
    assert "老师" in sheet
    assert "Never call the player 队长" in sheet
    assert "Sleepy but earnest" in sheet

from pathlib import Path

from app.rag.loaders import KnowledgeLoader


def test_knowledge_loader_loads_markdown_with_category(tmp_path: Path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "xiaokong_persona.md").write_text("# 小空\n温和但警觉。", encoding="utf-8")
    (knowledge_dir / "worldview.md").write_text("# 世界\n避难所资源紧张。", encoding="utf-8")

    docs = KnowledgeLoader(knowledge_dir).load()

    assert len(docs) == 2
    categories = {doc.metadata["category"] for doc in docs}
    assert categories == {"persona", "world"}
    assert {doc.metadata["source"] for doc in docs} == {"xiaokong_persona.md", "worldview.md"}


def test_knowledge_loader_classifies_mirdo_personality_bible_as_persona(tmp_path: Path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "mirdo_personality_bible.md").write_text("# Mirdo 人格\n老师称呼规则。", encoding="utf-8")

    docs = KnowledgeLoader(knowledge_dir).load()

    assert docs[0].metadata["category"] == "persona"

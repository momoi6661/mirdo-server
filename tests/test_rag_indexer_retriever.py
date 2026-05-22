from pathlib import Path

from app.rag.indexer import RAGIndexer
from app.rag.retriever import RAGRetriever


def test_rag_indexer_and_retriever_roundtrip(tmp_path: Path):
    knowledge_dir = tmp_path / "knowledge"
    chroma_dir = tmp_path / "chroma"
    knowledge_dir.mkdir()
    (knowledge_dir / "xiaokong_persona.md").write_text(
        "# 小空\n小空会认真管理便利站库存，记得玩家喜欢罐头汤。",
        encoding="utf-8",
    )
    (knowledge_dir / "survival.md").write_text(
        "# 生存\n净水片可以降低饮水风险，外出前应该检查背包。",
        encoding="utf-8",
    )

    indexer = RAGIndexer(knowledge_dir=knowledge_dir, chroma_dir=chroma_dir)
    result = indexer.ingest(clear_first=True)

    assert result["ok"] is True
    assert result["chunks_indexed"] >= 2

    retriever = RAGRetriever(chroma_dir=chroma_dir)
    hits = retriever.retrieve("小空会记得我喜欢什么食物？", top_k=2)

    assert hits
    assert any("罐头汤" in hit["text"] for hit in hits)
    assert all("source" in hit for hit in hits)

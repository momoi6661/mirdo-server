from pathlib import Path

from app.config import Settings
from app.rag.embeddings import FastEmbedEmbeddings, LocalHashEmbeddings, build_embeddings
from app.rag.indexer import RAGIndexer
from app.rag.loaders import KnowledgeLoader
from app.rag.retriever import RAGRetriever


def test_knowledge_loader_can_ingest_selected_project_tree(tmp_path: Path):
    project = tmp_path / "FPS"
    (project / "docs").mkdir(parents=True)
    (project / "ai").mkdir()
    (project / "scripts" / "character_ai").mkdir(parents=True)
    (project / "textures").mkdir()
    (project / "docs" / "outing.md").write_text("# 外出\n外出结算使用 /outing/resolve。", encoding="utf-8")
    (project / "ai" / "AI_INTERFACE_CN.md").write_text("# AI\nPOST /chat 返回 dialogue。", encoding="utf-8")
    (project / "scripts" / "character_ai" / "dialogue.gd").write_text(
        'func chat():\n\tprint("Mirdo 对话组件")\n',
        encoding="utf-8",
    )
    (project / "textures" / "noise.md").write_text("# 不应导入\n贴图说明不属于后端知识。", encoding="utf-8")

    docs = KnowledgeLoader(project, include_project_tree=True).load()

    sources = {doc.metadata["source"] for doc in docs}
    assert "docs/outing.md" in sources
    assert "ai/AI_INTERFACE_CN.md" in sources
    assert "scripts/character_ai/dialogue.gd" in sources
    assert "textures/noise.md" not in sources
    assert {doc.metadata["category"] for doc in docs} >= {"design_doc", "ai_contract", "character_ai_code"}


def test_build_embeddings_uses_provider_when_configured():
    settings = Settings(embedding_provider="openai", embedding_model="text-embedding-3-small")

    embeddings = build_embeddings(settings)

    assert embeddings.__class__.__name__ == "OpenAIEmbeddings"


def test_build_embeddings_defaults_to_better_local_hash_embedding():
    embeddings = build_embeddings(Settings(embedding_provider="local_hash"))

    assert isinstance(embeddings, LocalHashEmbeddings)
    assert embeddings.dimensions >= 768


def test_build_embeddings_supports_local_fastembed_model(tmp_path: Path):
    settings = Settings(
        embedding_provider="fastembed",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_cache_dir=str(tmp_path / "models"),
    )

    embeddings = build_embeddings(settings)

    assert isinstance(embeddings, FastEmbedEmbeddings)
    assert embeddings.model_name == "BAAI/bge-small-zh-v1.5"


def test_project_rag_retrieval_finds_outing_backend_contract(tmp_path: Path):
    project = tmp_path / "FPS"
    chroma_dir = tmp_path / "chroma"
    (project / "docs").mkdir(parents=True)
    (project / "levels" / "outing").mkdir(parents=True)
    (project / "docs" / "outing_ai_expedition_implementation.md").write_text(
        "# 外出地图\n外出结算接口是 POST /outing/resolve，返回 story、loot、health_damage。",
        encoding="utf-8",
    )
    (project / "levels" / "outing" / "outing_map_level_v3.gd").write_text(
        'const OUTING_AI_ENDPOINT_PATH := "/outing/resolve"\n',
        encoding="utf-8",
    )

    result = RAGIndexer(knowledge_dir=project, chroma_dir=chroma_dir, include_project_tree=True).ingest(clear_first=True)
    hits = RAGRetriever(chroma_dir=chroma_dir).retrieve("外出地图后端结算接口是什么？", top_k=3)

    assert result["documents_loaded"] == 2
    assert result["embedding_provider"] == "fastembed"
    assert any("/outing/resolve" in hit["text"] for hit in hits)
    assert all("score" in hit for hit in hits)

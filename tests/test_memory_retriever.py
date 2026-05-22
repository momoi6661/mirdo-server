from pathlib import Path

from langchain_core.documents import Document

from app.memory.retriever import MemoryRAGRetriever
from app.memory.store import MemoryStore
from app.rag.embeddings import LocalHashEmbeddings


def test_memory_rag_retriever_rehydrates_chroma_hit_outside_recent_sql_window(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    old_fact = store.upsert_memory_fact("session-a", "player", "likes", "清水", 0.9, 0)

    retriever = MemoryRAGRetriever(
        memory_store=store,
        chroma_dir=tmp_path / "chroma",
        embeddings=LocalHashEmbeddings(),
    )
    fake_store = _FakeVectorStore(old_fact.id)
    retriever._vector_store = fake_store

    for index in range(250):
        store.upsert_memory_fact("session-a", "player", "note", f"普通记录{index}", 0.5, 0)

    hits = retriever.retrieve("session-a", "老师平时喜欢喝什么？", top_k=5)

    assert any(hit["id"] == old_fact.id and hit["value"] == "清水" for hit in hits)


class _FakeVectorStore:
    def __init__(self, hit_fact_id: int) -> None:
        self._collection = _FakeCollection()
        self.hit_fact_id = hit_fact_id

    def similarity_search_with_relevance_scores(self, query: str, k: int = 4, filter=None):
        return [
            (
                Document(
                    page_content="player likes: 清水",
                    metadata={
                        "session_id": "session-a",
                        "memory_fact_id": self.hit_fact_id,
                    },
                ),
                0.95,
            )
        ]


class _FakeCollection:
    def upsert(self, documents, metadatas, ids) -> None:
        return None


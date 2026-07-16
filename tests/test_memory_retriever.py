from pathlib import Path
from app.memory.retriever import MemoryRAGRetriever
from app.memory.store import MemoryStore


def test_memory_retriever_is_session_scoped(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    store.upsert_memory_fact('a', '玩家', '喜欢', '罐头汤')
    store.upsert_memory_fact('b', '玩家', '喜欢', '矿泉水')
    hits = MemoryRAGRetriever(memory_store=store).retrieve('a', '喜欢什么', 5)
    assert [hit['value'] for hit in hits] == ['罐头汤']

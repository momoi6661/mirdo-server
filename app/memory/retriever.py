from __future__ import annotations
from typing import Any
from app.config import Settings
from .store import MemoryStore


class MemoryRAGRetriever:
    """Session-scoped SQLite retrieval facade; no vector database is used."""
    def __init__(self, *, memory_store: MemoryStore, settings: Settings | None = None, **_: Any) -> None:
        self.memory_store = memory_store
        self.settings = settings or Settings()

    def retrieve(self, session_id: str, query: str, top_k: int = 12) -> list[dict[str, Any]]:
        return [fact.to_dict() for fact in self.memory_store.search_memory_facts(session_id, query, limit=top_k)]

    def clear_session_index(self, session_id: str) -> int: return 0
    def clear_all_index(self) -> int: return 0
    clear_session_vectors = clear_session_index
    clear_all_vectors = clear_all_index
    def count_session_vectors(self, session_id: str) -> int: return len(self.memory_store.get_memory_facts(session_id, limit=200))

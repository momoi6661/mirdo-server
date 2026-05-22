from __future__ import annotations

from pathlib import Path
from typing import Any
import warnings

from langchain_chroma import Chroma
from langchain_core.documents import Document

from app.config import Settings
from app.rag.embeddings import build_embeddings

from .store import MemoryFact, MemoryStore

MEMORY_COLLECTION_NAME = "session_memory"


class MemoryRAGRetriever:
    """Embedding-backed long-term memory retrieval for one player's session.

    World knowledge RAG and memory RAG are intentionally separate Chroma
    collections. World knowledge is static project/design material; session
    memory is mutable player-specific facts such as preferences, names and
    promises. Retrieval is hybrid: semantic hits first, lexical/recent fallback
    from SQLite second.
    """

    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        chroma_dir: str | Path,
        settings: Settings | None = None,
        embeddings: Any | None = None,
    ) -> None:
        self.memory_store = memory_store
        self.chroma_dir = Path(chroma_dir)
        self.settings = settings or Settings()
        self.embeddings = embeddings or build_embeddings(self.settings)
        self._vector_store: Chroma | None = None
        self._session_versions: dict[str, str] = {}

    def retrieve(self, session_id: str, query: str, top_k: int = 12) -> list[dict[str, Any]]:
        clean_query = str(query or "").strip()
        safe_k = max(1, min(int(top_k), 50))
        facts = self.memory_store.get_memory_facts(session_id, limit=200)
        if not facts:
            return []
        if clean_query:
            self._sync_session(session_id, facts)
        by_id = {fact.id: fact for fact in facts}

        selected: list[MemoryFact] = []
        seen: set[int] = set()
        if clean_query:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    hits = self._store().similarity_search_with_relevance_scores(
                        clean_query,
                        k=min(safe_k, 20),
                        filter={"session_id": str(session_id or "default_session").strip() or "default_session"},
                    )
                missing_fact_ids: list[int] = []
                parsed_hits: list[int] = []
                for doc, _score in hits:
                    raw_id = doc.metadata.get("memory_fact_id", 0)
                    try:
                        fact_id = int(raw_id)
                    except (TypeError, ValueError):
                        continue
                    parsed_hits.append(fact_id)
                    if fact_id not in by_id:
                        missing_fact_ids.append(fact_id)
                if missing_fact_ids:
                    for fact in self.memory_store.get_memory_facts_by_ids(session_id, missing_fact_ids):
                        by_id[fact.id] = fact
                for fact_id in parsed_hits:
                    fact = by_id.get(fact_id)
                    if fact is None or fact.id in seen:
                        continue
                    selected.append(fact)
                    seen.add(fact.id)
                    if len(selected) >= safe_k:
                        break
            except Exception:
                # Chroma memory retrieval should never break gameplay dialogue.
                pass

        for fact in self.memory_store.search_memory_facts(session_id, clean_query, limit=safe_k):
            if fact.id in seen:
                continue
            selected.append(fact)
            seen.add(fact.id)
            if len(selected) >= safe_k:
                break
        return [fact.to_dict() for fact in selected[:safe_k]]

    def _sync_session(self, session_id: str, facts: list[MemoryFact]) -> None:
        clean_session = str(session_id or "default_session").strip() or "default_session"
        version = self._version(facts)
        if self._session_versions.get(clean_session) == version:
            return
        docs: list[str] = []
        metadatas: list[dict[str, Any]] = []
        ids: list[str] = []
        for fact in facts:
            docs.append(f"{fact.subject} {fact.predicate}: {fact.value}")
            metadatas.append(
                {
                    "session_id": clean_session,
                    "memory_fact_id": int(fact.id),
                    "subject": fact.subject,
                    "predicate": fact.predicate,
                    "value": fact.value,
                    "confidence": float(fact.confidence),
                    "updated_at": fact.updated_at,
                }
            )
            ids.append(self._chroma_id(fact.id))
        if docs:
            collection = self._store()._collection
            collection.upsert(documents=docs, metadatas=metadatas, ids=ids)
        self._session_versions[clean_session] = version


    def clear_session_vectors(self, session_id: str) -> int:
        clean_session = str(session_id or "default_session").strip() or "default_session"
        deleted = 0
        try:
            collection = self._store()._collection
            result = collection.get(where={"session_id": clean_session}, include=[])
            ids = list(result.get("ids", [])) if isinstance(result, dict) else []
            if ids:
                collection.delete(ids=ids)
                deleted = len(ids)
        except Exception:
            deleted = 0
        self._session_versions.pop(clean_session, None)
        return deleted

    def clear_all_vectors(self) -> int:
        deleted = 0
        try:
            collection = self._store()._collection
            result = collection.get(include=[])
            ids = list(result.get("ids", [])) if isinstance(result, dict) else []
            if ids:
                collection.delete(ids=ids)
                deleted = len(ids)
        except Exception:
            deleted = 0
        self._session_versions.clear()
        return deleted

    def count_session_vectors(self, session_id: str) -> int:
        clean_session = str(session_id or "default_session").strip() or "default_session"
        try:
            result = self._store()._collection.get(where={"session_id": clean_session}, include=[])
            ids = list(result.get("ids", [])) if isinstance(result, dict) else []
            return len(ids)
        except Exception:
            return 0

    def _store(self) -> Chroma:
        if self._vector_store is None:
            self.chroma_dir.mkdir(parents=True, exist_ok=True)
            self._vector_store = Chroma(
                collection_name=MEMORY_COLLECTION_NAME,
                embedding_function=self.embeddings,
                persist_directory=str(self.chroma_dir),
            )
        return self._vector_store

    @staticmethod
    def _chroma_id(memory_fact_id: int) -> str:
        return f"memory:{int(memory_fact_id)}"

    @staticmethod
    def _version(facts: list[MemoryFact]) -> str:
        if not facts:
            return "0"
        latest = max(str(fact.updated_at) for fact in facts)
        max_id = max(int(fact.id) for fact in facts)
        return f"{len(facts)}:{max_id}:{latest}"

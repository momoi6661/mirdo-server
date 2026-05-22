from __future__ import annotations

from pathlib import Path
import json
import shutil
from typing import Any
import warnings

from langchain_chroma import Chroma

from app.config import Settings

from .embeddings import build_embeddings
from .indexer import COLLECTION_NAME, READY_MARKER


class RAGRetriever:
    def __init__(self, chroma_dir: str | Path, *, settings: Settings | None = None) -> None:
        self.chroma_dir = Path(chroma_dir)
        self.settings = settings or Settings()
        self.embeddings = build_embeddings(self.settings)
        self._vector_store: Chroma | None = None
        self._store_factory = self._create_store
        self._store_factory_calls = 0

    def is_ready(self) -> bool:
        return self._ready()

    def _ready(self) -> bool:
        marker = self.chroma_dir / READY_MARKER
        return marker.exists() and marker.is_file()


    def status(self) -> dict[str, Any]:
        marker = self.chroma_dir / READY_MARKER
        marker_data: dict[str, Any] = {}
        if marker.exists() and marker.is_file():
            try:
                loaded = json.loads(marker.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    marker_data = loaded
            except (OSError, json.JSONDecodeError):
                marker_data = {}
        return {
            "ok": True,
            "ready": self.is_ready(),
            "collection": str(marker_data.get("collection", COLLECTION_NAME)),
            "chunks_indexed": int(marker_data.get("chunks_indexed", 0) or 0),
            "embedding_provider": str(marker_data.get("embedding_provider", getattr(self.settings, "embedding_provider", ""))),
            "include_project_tree": bool(marker_data.get("include_project_tree", False)),
            "updated_at": str(marker_data.get("updated_at", "")),
            "chroma_dir": str(self.chroma_dir),
        }

    def clear(self) -> dict[str, Any]:
        existed = self.chroma_dir.exists()
        vectors_deleted = 0
        errors: list[str] = []
        if self._vector_store is not None:
            try:
                collection = self._vector_store._collection
                result = collection.get(include=[])
                ids = list(result.get("ids", [])) if isinstance(result, dict) else []
                if ids:
                    collection.delete(ids=ids)
                    vectors_deleted = len(ids)
            except Exception as exc:  # Chroma/HNSW can keep Windows file handles open.
                errors.append(str(exc))
            self._vector_store = None
        marker = self.chroma_dir / READY_MARKER
        if marker.exists():
            try:
                marker.unlink()
            except OSError as exc:
                errors.append(str(exc))
        try:
            if existed and any(self.chroma_dir.iterdir()):
                shutil.rmtree(self.chroma_dir)
        except OSError as exc:
            errors.append(str(exc))
        self.chroma_dir.mkdir(parents=True, exist_ok=True)
        return {
            "ok": len(errors) == 0 or not (self.chroma_dir / READY_MARKER).exists(),
            "ready": False,
            "cleared": existed,
            "vectors_deleted": vectors_deleted,
            "errors": errors,
            "chroma_dir": str(self.chroma_dir),
            "collection": COLLECTION_NAME,
        }

    def retrieve(self, query: str, top_k: int = 4) -> list[dict[str, Any]]:
        clean = str(query or "").strip()
        if not clean or not self.is_ready():
            return []
        store = self._store()
        safe_k = max(1, min(int(top_k), 20))
        if hasattr(store, "similarity_search_with_relevance_scores"):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                docs_with_scores = store.similarity_search_with_relevance_scores(clean, k=safe_k)
        else:
            docs_with_scores = [(doc, 0.0) for doc in store.similarity_search(clean, k=safe_k)]
        hits: list[dict[str, Any]] = []
        for doc, score in docs_with_scores:
            hits.append(
                {
                    "text": doc.page_content,
                    "source": str(doc.metadata.get("source", "")),
                    "category": str(doc.metadata.get("category", "")),
                    "score": max(0.0, min(float(score), 1.0)),
                }
            )
        return hits

    def _store(self) -> Chroma:
        if self._vector_store is None:
            self._store_factory_calls += 1
            self._vector_store = self._store_factory()
        return self._vector_store

    def _create_store(self) -> Chroma:
        return Chroma(
            collection_name=COLLECTION_NAME,
            embedding_function=self.embeddings,
            persist_directory=str(self.chroma_dir),
        )

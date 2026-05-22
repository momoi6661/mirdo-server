from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import Settings

from .embeddings import build_embeddings
from .loaders import KnowledgeLoader

COLLECTION_NAME = "world_knowledge"
READY_MARKER = ".rag_ready.json"


class RAGIndexer:
    def __init__(
        self,
        knowledge_dir: str | Path,
        chroma_dir: str | Path,
        *,
        settings: Settings | None = None,
        include_project_tree: bool | None = None,
    ) -> None:
        self.knowledge_dir = Path(knowledge_dir)
        self.chroma_dir = Path(chroma_dir)
        self.settings = settings or Settings()
        self.include_project_tree = bool(self.settings.rag_include_project_tree if include_project_tree is None else include_project_tree)
        self.embeddings = build_embeddings(self.settings)

    def ingest(self, clear_first: bool = False) -> dict:
        if clear_first and self.chroma_dir.exists():
            shutil.rmtree(self.chroma_dir)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)
        self._clear_ready_marker()

        raw_docs = KnowledgeLoader(self.knowledge_dir, include_project_tree=self.include_project_tree).load()
        if not raw_docs:
            result = {
                "ok": True,
                "documents_loaded": 0,
                "chunks_indexed": 0,
                "collection": COLLECTION_NAME,
                "embedding_provider": self._embedding_provider_name(),
            }
            return result

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=900,
            chunk_overlap=160,
            separators=["\n## ", "\n### ", "\nfunc ", "\nclass ", "\n\n", "\n", "。", "，", " ", ""],
        )
        chunks = splitter.split_documents(raw_docs)
        ids = [f"{doc.metadata.get('source', 'doc')}:{idx}:{uuid4().hex[:8]}" for idx, doc in enumerate(chunks)]

        vector_store = Chroma(
            collection_name=COLLECTION_NAME,
            embedding_function=self.embeddings,
            persist_directory=str(self.chroma_dir),
        )
        vector_store.add_documents(chunks, ids=ids)

        result = {
            "ok": True,
            "documents_loaded": len(raw_docs),
            "chunks_indexed": len(chunks),
            "collection": COLLECTION_NAME,
            "embedding_provider": self._embedding_provider_name(),
            "include_project_tree": self.include_project_tree,
        }
        self._write_ready_marker(result)
        return result

    def _ready_marker_path(self) -> Path:
        return self.chroma_dir / READY_MARKER

    def _clear_ready_marker(self) -> None:
        marker = self._ready_marker_path()
        if marker.exists():
            marker.unlink()

    def _write_ready_marker(self, result: dict) -> None:
        marker = self._ready_marker_path()
        marker.write_text(
            json.dumps(
                {
                    "ready": True,
                    "collection": COLLECTION_NAME,
                    "chunks_indexed": int(result.get("chunks_indexed", 0)),
                    "embedding_provider": str(result.get("embedding_provider", "")),
                    "include_project_tree": bool(result.get("include_project_tree", False)),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _embedding_provider_name(self) -> str:
        return str(getattr(self.settings, "embedding_provider", "local_hash") or "local_hash").strip() or "local_hash"

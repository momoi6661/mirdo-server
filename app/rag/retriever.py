from __future__ import annotations
from pathlib import Path
from .sqlite_store import SQLiteRAGStore
from app.config import Settings


class RAGRetriever:
    def __init__(self, _legacy_path: str | Path | None = None, *, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.store = SQLiteRAGStore(self.settings.rag_db, self.settings.knowledge_dir, include_project_tree=self.settings.rag_include_project_tree)
        # 后端启动时用当前 Markdown 重建小型 FTS 索引，避免删除旧人设后仍命中陈旧数据库。
        self.store.ingest()
    def is_ready(self) -> bool: return bool(self.status()['ready'])
    def status(self): return self.store.status()
    def clear(self): return self.store.clear()
    def retrieve(self, query: str, top_k: int = 4): return self.store.retrieve(query, top_k)

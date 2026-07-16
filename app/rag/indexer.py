from .sqlite_store import SQLiteRAGStore

# 旧路由仍从此模块导入 RAGIndexer；实际实现只有 SQLiteRAGStore 一份。
RAGIndexer = SQLiteRAGStore

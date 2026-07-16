from pathlib import Path
from app.rag.sqlite_store import SQLiteRAGStore


def test_rag_uses_sqlite_fts5_without_embeddings(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / 'world.md').write_text('Mirdo 会守护避难所的食物和水。', encoding='utf-8')
    store = SQLiteRAGStore(tmp_path / 'rag.sqlite3', knowledge)
    result = store.ingest(clear_first=True)
    assert result['retrieval'] == 'sqlite_fts5'
    assert store.retrieve('避难所 食物', 1)[0]['source'] == 'world.md'
    assert store.retrieve('完全无关的问题', 1) == []

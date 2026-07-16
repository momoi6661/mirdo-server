from pathlib import Path
from app.rag.sqlite_store import SQLiteRAGStore


def test_ingest_clear_and_status(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / 'rules.md').write_text('外出时需要注意丧尸风险。', encoding='utf-8')
    store = SQLiteRAGStore(tmp_path / 'rag.sqlite3', knowledge)
    assert store.ingest()['chunks_indexed'] == 1
    assert store.status()['ready'] is True
    assert store.clear()['documents_deleted'] == 1
    assert store.status()['ready'] is False

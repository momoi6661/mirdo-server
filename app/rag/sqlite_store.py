from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .loaders import KnowledgeLoader


class SQLiteRAGStore:
    """Dependency-free FTS5 world-knowledge store."""

    def __init__(self, db_path: str | Path, knowledge_dir: str | Path, *, include_project_tree: bool = False) -> None:
        self.db_path = Path(db_path)
        self.knowledge_dir = Path(knowledge_dir)
        self.include_project_tree = include_project_tree

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript('''
                create table if not exists knowledge_chunks (
                    id integer primary key, source text not null, category text not null,
                    text text not null, updated_at text not null
                );
                create virtual table if not exists knowledge_fts using fts5(
                    text, source unindexed, category unindexed, content='knowledge_chunks', content_rowid='id'
                );
                create trigger if not exists knowledge_ai after insert on knowledge_chunks begin
                    insert into knowledge_fts(rowid,text,source,category) values(new.id,new.text,new.source,new.category);
                end;
                create trigger if not exists knowledge_ad after delete on knowledge_chunks begin
                    insert into knowledge_fts(knowledge_fts,rowid,text,source,category) values('delete',old.id,old.text,old.source,old.category);
                end;
            ''')

    def ingest(self, clear_first: bool = False) -> dict[str, Any]:
        self.initialize()
        docs = KnowledgeLoader(self.knowledge_dir, include_project_tree=self.include_project_tree).load()
        chunks = [(doc.metadata.get('source',''), doc.metadata.get('category','world'), chunk) for doc in docs for chunk in self._chunks(doc.page_content)]
        with self._connect() as conn:
            conn.execute('delete from knowledge_chunks')
            now = datetime.now(timezone.utc).isoformat()
            conn.executemany('insert into knowledge_chunks(source,category,text,updated_at) values(?,?,?,?)', [(s,c,t,now) for s,c,t in chunks])
        return {'ok': True, 'documents_loaded': len(docs), 'chunks_indexed': len(chunks), 'collection': 'world_knowledge', 'retrieval': 'sqlite_fts5', 'include_project_tree': self.include_project_tree}

    def retrieve(self, query: str, top_k: int = 4) -> list[dict[str, Any]]:
        terms = self._fts_query(query)
        if not terms:
            return []
        with self._connect() as conn:
            rows = conn.execute('select k.text,k.source,k.category,bm25(knowledge_fts) score from knowledge_fts join knowledge_chunks k on k.id=knowledge_fts.rowid where knowledge_fts match ? order by score limit ?', (terms, max(1,min(int(top_k),20)))).fetchall()
        if not rows:
            # CJK tokenizer differences may make FTS miss a real match. Use lexical fallback,
            # but never return arbitrary first documents: irrelevant persona text is worse than no result.
            return self._lexical_fallback(query, top_k)
        return [{'text': r['text'], 'source': r['source'], 'category': r['category'], 'score': max(0.0, 1.0 / (1.0 + abs(float(r['score']))))} for r in rows]

    def _lexical_fallback(self, query: str, top_k: int) -> list[dict[str, Any]]:
        query_tokens = self._tokens(query)
        if not query_tokens:
            return []
        with self._connect() as conn:
            rows = conn.execute('select text,source,category from knowledge_chunks').fetchall()
        ranked: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            text = str(row['text']).lower()
            score = sum(1 for token in query_tokens if token in text)
            if score:
                ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [
            {'text': row['text'], 'source': row['source'], 'category': row['category'], 'score': float(score)}
            for score, row in ranked[: max(1, min(int(top_k), 20))]
        ]

    def status(self) -> dict[str, Any]:
        self.initialize()
        with self._connect() as conn:
            count = int(conn.execute('select count(*) from knowledge_chunks').fetchone()[0])
        return {'ok': True, 'ready': bool(count), 'collection': 'world_knowledge', 'chunks_indexed': count, 'retrieval': 'sqlite_fts5', 'rag_db': str(self.db_path)}

    def clear(self) -> dict[str, Any]:
        self.initialize()
        with self._connect() as conn:
            deleted = conn.execute('delete from knowledge_chunks').rowcount
        return {'ok': True, 'ready': False, 'cleared': True, 'documents_deleted': max(0, int(deleted)), 'collection': 'world_knowledge', 'retrieval': 'sqlite_fts5'}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _chunks(text: str) -> list[str]:
        clean = str(text or '').strip()
        return [clean[i:i + 900] for i in range(0, len(clean), 740)] if clean else []

    @staticmethod
    def _fts_query(query: str) -> str:
        source = str(query or '').lower()
        tokens = re.findall(r'[\w\u4e00-\u9fff]{2,}', source)
        cjk = ''.join(re.findall(r'[\u4e00-\u9fff]', source))
        tokens.extend(cjk[index:index + 2] for index in range(max(0, len(cjk) - 1)))
        return ' OR '.join(f'"{token.replace(chr(34), "")}"' for token in dict.fromkeys(tokens[:24]))

    @staticmethod
    def _tokens(text: str) -> set[str]:
        source = str(text or '').lower()
        tokens = set(re.findall(r'[\w\u4e00-\u9fff]{2,}', source))
        cjk = ''.join(re.findall(r'[\u4e00-\u9fff]', source))
        tokens.update(cjk[index:index + 2] for index in range(max(0, len(cjk) - 1)))
        return tokens

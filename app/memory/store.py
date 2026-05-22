from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Turn:
    id: int
    session_id: str
    role: str
    content: str
    payload: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class MemoryFact:
    id: int
    session_id: str
    subject: str
    predicate: str
    value: str
    confidence: float
    source_turn_id: int
    active: bool
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "confidence": self.confidence,
            "source_turn_id": self.source_turn_id,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                pragma foreign_keys = on;

                create table if not exists sessions (
                    session_id text primary key,
                    summary text not null default '',
                    summary_turn_id integer not null default 0,
                    metadata_json text not null default '{}',
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists turns (
                    id integer primary key autoincrement,
                    session_id text not null,
                    role text not null check(role in ('user', 'assistant', 'system')),
                    content text not null,
                    payload_json text not null default '{}',
                    created_at text not null,
                    foreign key(session_id) references sessions(session_id) on delete cascade
                );

                create index if not exists idx_turns_session_id_id on turns(session_id, id);

                create table if not exists memory_facts (
                    id integer primary key autoincrement,
                    session_id text not null,
                    subject text not null,
                    predicate text not null,
                    value text not null,
                    confidence real not null default 0.7,
                    source_turn_id integer not null default 0,
                    active integer not null default 1,
                    created_at text not null,
                    updated_at text not null,
                    unique(session_id, subject, predicate, value),
                    foreign key(session_id) references sessions(session_id) on delete cascade
                );

                create index if not exists idx_memory_facts_session_active
                    on memory_facts(session_id, active);

                create table if not exists memory_embeddings (
                    memory_fact_id integer primary key,
                    chroma_id text not null unique,
                    collection text not null default 'session_memory',
                    updated_at text not null,
                    foreign key(memory_fact_id) references memory_facts(id) on delete cascade
                );
                """
            )

    def health(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("select 1")
            return True
        except sqlite3.Error:
            return False

    def add_turn(self, session_id: str, role: str, content: str, payload: dict[str, Any] | None = None) -> Turn:
        clean_session = self._clean_session_id(session_id)
        clean_role = str(role).strip()
        if clean_role not in {"user", "assistant", "system"}:
            raise ValueError(f"invalid turn role: {role}")
        clean_content = str(content or "").strip()
        now = self._now()
        payload_json = self._json_dumps(payload or {})

        with self._connect() as conn:
            self._ensure_session(conn, clean_session, now)
            cursor = conn.execute(
                """
                insert into turns(session_id, role, content, payload_json, created_at)
                values (?, ?, ?, ?, ?)
                """,
                (clean_session, clean_role, clean_content, payload_json, now),
            )
            conn.execute(
                "update sessions set updated_at = ? where session_id = ?",
                (now, clean_session),
            )
            turn_id = int(cursor.lastrowid)
            row = conn.execute(
                "select * from turns where id = ?",
                (turn_id,),
            ).fetchone()
        return self._turn_from_row(row)

    def get_recent_turns(self, session_id: str, limit: int = 20) -> list[Turn]:
        clean_session = self._clean_session_id(session_id)
        safe_limit = max(0, min(int(limit), 200))
        if safe_limit == 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                select * from (
                    select * from turns
                    where session_id = ?
                    order by id desc
                    limit ?
                ) ordered
                order by id asc
                """,
                (clean_session, safe_limit),
            ).fetchall()
        return [self._turn_from_row(row) for row in rows]

    def upsert_memory_fact(
        self,
        session_id: str,
        subject: str,
        predicate: str,
        value: str,
        confidence: float = 0.7,
        source_turn_id: int = 0,
    ) -> MemoryFact:
        clean_session = self._clean_session_id(session_id)
        clean_subject = str(subject or "").strip() or "unknown"
        clean_predicate = str(predicate or "").strip() or "related_to"
        clean_value = str(value or "").strip()
        if not clean_value:
            raise ValueError("memory fact value must not be empty")
        safe_confidence = max(0.0, min(float(confidence), 1.0))
        now = self._now()

        with self._connect() as conn:
            self._ensure_session(conn, clean_session, now)
            conn.execute(
                """
                insert into memory_facts(
                    session_id, subject, predicate, value, confidence,
                    source_turn_id, active, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, 1, ?, ?)
                on conflict(session_id, subject, predicate, value)
                do update set
                    confidence = excluded.confidence,
                    source_turn_id = excluded.source_turn_id,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    clean_session,
                    clean_subject,
                    clean_predicate,
                    clean_value,
                    safe_confidence,
                    int(source_turn_id),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                select * from memory_facts
                where session_id = ? and subject = ? and predicate = ? and value = ?
                """,
                (clean_session, clean_subject, clean_predicate, clean_value),
            ).fetchone()
        return self._fact_from_row(row)

    def get_memory_facts(self, session_id: str, limit: int = 50) -> list[MemoryFact]:
        clean_session = self._clean_session_id(session_id)
        safe_limit = max(0, min(int(limit), 200))
        if safe_limit == 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                select * from memory_facts
                where session_id = ? and active = 1
                order by updated_at desc, id desc
                limit ?
                """,
                (clean_session, safe_limit),
            ).fetchall()
        return [self._fact_from_row(row) for row in rows]

    def search_memory_facts(self, session_id: str, query: str, limit: int = 12) -> list[MemoryFact]:
        """Return active facts ranked for the current player input.

        This is a lightweight lexical retrieval layer for long-term memory. It
        keeps memory retrieval cheap enough for the game loop while avoiding the
        old behavior where only the newest facts were injected. Recent facts are
        still mixed in as a fallback so newly learned preferences show up even
        if the wording differs.
        """

        clean_session = self._clean_session_id(session_id)
        safe_limit = max(0, min(int(limit), 50))
        if safe_limit == 0:
            return []

        with self._connect() as conn:
            rows = conn.execute(
                """
                select * from memory_facts
                where session_id = ? and active = 1
                order by updated_at desc, id desc
                limit 200
                """,
                (clean_session,),
            ).fetchall()

        facts = [self._fact_from_row(row) for row in rows]
        if not facts:
            return []

        query_tokens = self._memory_tokens(query)
        scored: list[tuple[float, int, MemoryFact]] = []
        for index, fact in enumerate(facts):
            fact_text = f"{fact.subject} {fact.predicate} {fact.value}"
            fact_tokens = self._memory_tokens(fact_text)
            overlap = len(query_tokens & fact_tokens)
            value_hit = 1 if fact.value and str(fact.value) in str(query or "") else 0
            predicate_boost = 0.0
            if any(token in query_tokens for token in {"喜欢", "爱吃", "爱喝", "偏好"}):
                if fact.predicate == "likes":
                    predicate_boost = 3.0
                elif fact.predicate == "dislikes":
                    predicate_boost = 0.6
            elif any(token in query_tokens for token in {"讨厌", "不喜", "不喜欢"}):
                if fact.predicate == "dislikes":
                    predicate_boost = 3.0
                elif fact.predicate == "likes":
                    predicate_boost = 0.4
            elif any(token in query_tokens for token in {"名字", "叫我", "我叫"}):
                if fact.predicate == "name":
                    predicate_boost = 3.0
            elif any(token in query_tokens for token in {"记得", "记住", "承诺"}) and fact.predicate in {"likes", "dislikes", "name", "note"}:
                predicate_boost = 0.8
            recency_boost = max(0.0, 0.3 - index * 0.01)
            score = overlap * 1.0 + value_hit * 2.0 + predicate_boost + fact.confidence * 0.25 + recency_boost
            scored.append((score, -index, fact))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected: list[MemoryFact] = []
        seen: set[int] = set()
        for score, _neg_index, fact in scored:
            if score <= 0.35 and len(selected) >= max(3, safe_limit // 3):
                continue
            selected.append(fact)
            seen.add(fact.id)
            if len(selected) >= safe_limit:
                break

        # Always blend in a few most recent facts; this protects new memories
        # from being missed by lexical mismatch while preserving the ranked cap.
        for fact in facts[: min(4, safe_limit)]:
            if len(selected) >= safe_limit:
                break
            if fact.id in seen:
                continue
            selected.append(fact)
            seen.add(fact.id)
        return selected[:safe_limit]



    def get_latest_turn_id(self, session_id: str) -> int:
        clean_session = self._clean_session_id(session_id)
        with self._connect() as conn:
            row = conn.execute(
                "select coalesce(max(id), 0) as latest_id from turns where session_id = ?",
                (clean_session,),
            ).fetchone()
        return 0 if row is None else int(row["latest_id"])

    def fork_session(self, source_session_id: str, checkpoint_turn_id: int, new_session_id: str) -> dict[str, Any]:
        source_session = self._clean_session_id(source_session_id)
        target_session = self._clean_session_id(new_session_id)
        safe_checkpoint = max(0, int(checkpoint_turn_id))
        now = self._now()
        copied_turns = 0
        copied_facts = 0
        with self._connect() as conn:
            self._ensure_session(conn, target_session, now)
            turn_rows = conn.execute(
                """
                select * from turns
                where session_id = ? and id <= ?
                order by id asc
                """,
                (source_session, safe_checkpoint),
            ).fetchall()
            turn_id_map: dict[int, int] = {}
            for row in turn_rows:
                cursor = conn.execute(
                    """
                    insert into turns(session_id, role, content, payload_json, created_at)
                    values (?, ?, ?, ?, ?)
                    """,
                    (target_session, str(row["role"]), str(row["content"]), str(row["payload_json"]), str(row["created_at"])),
                )
                turn_id_map[int(row["id"])] = int(cursor.lastrowid)
                copied_turns += 1

            fact_rows = conn.execute(
                """
                select * from memory_facts
                where session_id = ? and active = 1 and (source_turn_id = 0 or source_turn_id <= ?)
                order by id asc
                """,
                (source_session, safe_checkpoint),
            ).fetchall()
            for row in fact_rows:
                source_turn_id = int(row["source_turn_id"])
                mapped_turn_id = int(turn_id_map.get(source_turn_id, 0 if source_turn_id <= 0 else source_turn_id))
                conn.execute(
                    """
                    insert into memory_facts(
                        session_id, subject, predicate, value, confidence,
                        source_turn_id, active, created_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    on conflict(session_id, subject, predicate, value)
                    do update set
                        confidence = excluded.confidence,
                        source_turn_id = excluded.source_turn_id,
                        active = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        target_session,
                        str(row["subject"]),
                        str(row["predicate"]),
                        str(row["value"]),
                        float(row["confidence"]),
                        mapped_turn_id,
                        str(row["created_at"]),
                        str(row["updated_at"]),
                    ),
                )
                copied_facts += 1
            conn.execute(
                "update sessions set updated_at = ? where session_id = ?",
                (now, target_session),
            )
        return {
            "ok": True,
            "source_session_id": source_session,
            "session_id": target_session,
            "checkpoint_turn_id": safe_checkpoint,
            "turns_copied": copied_turns,
            "facts_copied": copied_facts,
        }

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select
                    s.session_id,
                    s.summary,
                    s.summary_turn_id,
                    s.created_at,
                    s.updated_at,
                    coalesce(t.turn_count, 0) as turn_count,
                    coalesce(f.memory_fact_count, 0) as memory_fact_count
                from sessions s
                left join (
                    select session_id, count(*) as turn_count
                    from turns
                    group by session_id
                ) t on t.session_id = s.session_id
                left join (
                    select session_id, count(*) as memory_fact_count
                    from memory_facts
                    where active = 1
                    group by session_id
                ) f on f.session_id = s.session_id
                order by s.updated_at desc, s.session_id asc
                limit ?
                """,
                (safe_limit,),
            ).fetchall()
        return [
            {
                "session_id": str(row["session_id"]),
                "summary": str(row["summary"]),
                "summary_turn_id": int(row["summary_turn_id"]),
                "turn_count": int(row["turn_count"]),
                "memory_fact_count": int(row["memory_fact_count"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def delete_memory_fact(self, session_id: str, fact_id: int) -> dict[str, Any]:
        clean_session = self._clean_session_id(session_id)
        safe_fact_id = int(fact_id)
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                "select id from memory_facts where session_id = ? and id = ? and active = 1",
                (clean_session, safe_fact_id),
            ).fetchone()
            if row is None:
                deleted = False
            else:
                conn.execute(
                    "update memory_facts set active = 0, updated_at = ? where session_id = ? and id = ?",
                    (now, clean_session, safe_fact_id),
                )
                conn.execute(
                    "delete from memory_embeddings where memory_fact_id = ?",
                    (safe_fact_id,),
                )
                conn.execute(
                    "update sessions set updated_at = ? where session_id = ?",
                    (now, clean_session),
                )
                deleted = True
        return {
            "ok": True,
            "session_id": clean_session,
            "fact_id": safe_fact_id,
            "deleted": deleted,
        }

    def get_session_history(self, session_id: str, limit: int = 40) -> dict[str, Any]:
        clean_session = self._clean_session_id(session_id)
        turns = self.get_recent_turns(clean_session, limit)
        return {
            "ok": True,
            "session_id": clean_session,
            "turns": [turn.to_dict() for turn in turns],
        }

    def get_session_snapshot(self, session_id: str, recent_limit: int = 20) -> dict[str, Any]:
        clean_session = self._clean_session_id(session_id)
        with self._connect() as conn:
            session = conn.execute(
                "select * from sessions where session_id = ?",
                (clean_session,),
            ).fetchone()
        return {
            "ok": True,
            "session_id": clean_session,
            "summary": "" if session is None else str(session["summary"]),
            "summary_turn_id": 0 if session is None else int(session["summary_turn_id"]),
            "recent_turns": [turn.to_dict() for turn in self.get_recent_turns(clean_session, recent_limit)],
            "memory_facts": [fact.to_dict() for fact in self.get_memory_facts(clean_session)],
        }

    def clear_session(self, session_id: str) -> dict[str, Any]:
        clean_session = self._clean_session_id(session_id)
        with self._connect() as conn:
            turns_deleted = conn.execute(
                "delete from turns where session_id = ?",
                (clean_session,),
            ).rowcount
            facts_deleted = conn.execute(
                "delete from memory_facts where session_id = ?",
                (clean_session,),
            ).rowcount
            conn.execute(
                "delete from sessions where session_id = ?",
                (clean_session,),
            )
        return {
            "ok": True,
            "session_id": clean_session,
            "turns_deleted": max(0, int(turns_deleted)),
            "facts_deleted": max(0, int(facts_deleted)),
        }

    def clear_all(self) -> dict[str, Any]:
        with self._connect() as conn:
            turns_deleted = conn.execute("delete from turns").rowcount
            facts_deleted = conn.execute("delete from memory_facts").rowcount
            conn.execute("delete from sessions")
        return {
            "ok": True,
            "clear_all": True,
            "turns_deleted": max(0, int(turns_deleted)),
            "facts_deleted": max(0, int(facts_deleted)),
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        return conn

    def _ensure_session(self, conn: sqlite3.Connection, session_id: str, now: str) -> None:
        conn.execute(
            """
            insert into sessions(session_id, summary, summary_turn_id, metadata_json, created_at, updated_at)
            values (?, '', 0, '{}', ?, ?)
            on conflict(session_id) do update set updated_at = excluded.updated_at
            """,
            (session_id, now, now),
        )


    @staticmethod
    def _memory_tokens(text: str) -> set[str]:
        clean = str(text or "").lower()
        tokens: set[str] = set()
        buffer = ""
        for ch in clean:
            if "\u4e00" <= ch <= "\u9fff":
                if buffer:
                    tokens.add(buffer)
                    buffer = ""
                tokens.add(ch)
            elif ch.isalnum():
                buffer += ch
            else:
                if buffer:
                    tokens.add(buffer)
                    buffer = ""
        if buffer:
            tokens.add(buffer)
        # Add short CJK bigrams for better Chinese recall without an embedding call.
        cjk = [ch for ch in clean if "\u4e00" <= ch <= "\u9fff"]
        for idx in range(max(0, len(cjk) - 1)):
            tokens.add(cjk[idx] + cjk[idx + 1])
        return tokens

    @staticmethod
    def _clean_session_id(session_id: str) -> str:
        clean = str(session_id or "").strip()
        return clean or "default_session"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _json_dumps(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _json_loads(value: str) -> dict[str, Any]:
        try:
            loaded = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _turn_from_row(self, row: sqlite3.Row) -> Turn:
        return Turn(
            id=int(row["id"]),
            session_id=str(row["session_id"]),
            role=str(row["role"]),
            content=str(row["content"]),
            payload=self._json_loads(str(row["payload_json"])),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _fact_from_row(row: sqlite3.Row) -> MemoryFact:
        return MemoryFact(
            id=int(row["id"]),
            session_id=str(row["session_id"]),
            subject=str(row["subject"]),
            predicate=str(row["predicate"]),
            value=str(row["value"]),
            confidence=float(row["confidence"]),
            source_turn_id=int(row["source_turn_id"]),
            active=bool(row["active"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

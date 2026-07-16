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
    kind: str = "fact"
    importance: float = 0.5

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
            "kind": self.kind,
            "importance": self.importance,
        }


@dataclass(frozen=True)
class NavigationTask:
    """一个等待 Godot 真实导航结果的短任务，不混入长期记忆。"""

    task_id: str
    session_id: str
    goal: str
    command: str
    target_ref: str
    status: str
    last_event: str
    last_result: dict[str, Any]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "command": self.command,
            "target_ref": self.target_ref,
            "status": self.status,
            "last_event": self.last_event,
            "last_result": self.last_result,
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
                    kind text not null default 'fact',
                    importance real not null default 0.5,
                    created_at text not null,
                    updated_at text not null,
                    unique(session_id, subject, predicate, value),
                    foreign key(session_id) references sessions(session_id) on delete cascade
                );

                create index if not exists idx_memory_facts_session_active
                    on memory_facts(session_id, active);

                create virtual table if not exists memory_fts using fts5(
                    subject, predicate, value, session_id unindexed, content='memory_facts', content_rowid='id'
                );
                create trigger if not exists memory_ai after insert on memory_facts begin
                    insert into memory_fts(rowid,subject,predicate,value,session_id) values(new.id,new.subject,new.predicate,new.value,new.session_id);
                end;
                create trigger if not exists memory_au after update on memory_facts begin
                    insert into memory_fts(memory_fts,rowid,subject,predicate,value,session_id) values('delete',old.id,old.subject,old.predicate,old.value,old.session_id);
                    insert into memory_fts(rowid,subject,predicate,value,session_id) values(new.id,new.subject,new.predicate,new.value,new.session_id);
                end;
                create trigger if not exists memory_ad after delete on memory_facts begin
                    insert into memory_fts(memory_fts,rowid,subject,predicate,value,session_id) values('delete',old.id,old.subject,old.predicate,old.value,old.session_id);
                end;

                create table if not exists story_events (
                    id integer primary key autoincrement,
                    session_id text not null,
                    kind text not null,
                    summary text not null,
                    importance real not null default 0.5,
                    source_turn_id integer not null default 0,
                    metadata_json text not null default '{}',
                    created_at text not null,
                    foreign key(session_id) references sessions(session_id) on delete cascade
                );
                create index if not exists idx_story_events_session_id_id on story_events(session_id, id desc);

                create table if not exists navigation_tasks (
                    task_id text primary key,
                    session_id text not null,
                    goal text not null,
                    command text not null,
                    target_ref text not null,
                    status text not null check(status in ('waiting', 'succeeded', 'failed', 'cancelled')),
                    last_event text not null default '',
                    last_result_json text not null default '{}',
                    created_at text not null,
                    updated_at text not null,
                    foreign key(session_id) references sessions(session_id) on delete cascade
                );
                create index if not exists idx_navigation_tasks_session_updated
                    on navigation_tasks(session_id, updated_at desc);
                """
            )
            # 老存档没有新字段时原地升级，不影响已有对话与事实。
            columns = {row[1] for row in conn.execute("pragma table_info(memory_facts)")}
            if "kind" not in columns:
                conn.execute("alter table memory_facts add column kind text not null default 'fact'")
            if "importance" not in columns:
                conn.execute("alter table memory_facts add column importance real not null default 0.5")
            story_columns = {row[1] for row in conn.execute("pragma table_info(story_events)")}
            if "metadata_json" not in story_columns:
                conn.execute("alter table story_events add column metadata_json text not null default '{}'")
            # Triggers only cover writes after migration; idempotently backfill old facts.
            conn.execute("insert into memory_fts(rowid,subject,predicate,value,session_id) select id,subject,predicate,value,session_id from memory_facts where id not in (select rowid from memory_fts)")

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

    def get_session_summary(self, session_id: str) -> tuple[str, int]:
        """返回已压缩的旧对话及其覆盖到的最后一个 turn id。"""
        with self._connect() as conn:
            row = conn.execute(
                "select summary, summary_turn_id from sessions where session_id = ?",
                (self._clean_session_id(session_id),),
            ).fetchone()
        return ("", 0) if row is None else (str(row["summary"]), int(row["summary_turn_id"]))

    def get_turns_after(self, session_id: str, turn_id: int, limit: int = 24) -> list[Turn]:
        """读取尚未压缩到摘要中的连续对话，供摘要 Agent 使用。"""
        with self._connect() as conn:
            rows = conn.execute(
                "select * from turns where session_id = ? and id > ? order by id asc limit ?",
                (self._clean_session_id(session_id), max(0, int(turn_id)), max(1, min(int(limit), 100))),
            ).fetchall()
        return [self._turn_from_row(row) for row in rows]

    def update_session_summary(self, session_id: str, summary: str, summary_turn_id: int) -> None:
        """原子替换会话摘要；原始 turns 保留，摘要只用于节省上下文窗口。"""
        clean_summary = str(summary or "").strip()[:2000]
        if not clean_summary:
            return
        now = self._now()
        with self._connect() as conn:
            self._ensure_session(conn, self._clean_session_id(session_id), now)
            conn.execute(
                "update sessions set summary = ?, summary_turn_id = ?, updated_at = ? where session_id = ?",
                (clean_summary, max(0, int(summary_turn_id)), now, self._clean_session_id(session_id)),
            )

    def upsert_memory_fact(
        self,
        session_id: str,
        subject: str,
        predicate: str,
        value: str,
        confidence: float = 0.7,
        source_turn_id: int = 0,
        kind: str = "fact",
        importance: float = 0.5,
    ) -> MemoryFact:
        clean_session = self._clean_session_id(session_id)
        clean_subject = str(subject or "").strip() or "unknown"
        clean_predicate = str(predicate or "").strip() or "related_to"
        clean_value = str(value or "").strip()
        if not clean_value:
            raise ValueError("memory fact value must not be empty")
        safe_confidence = max(0.0, min(float(confidence), 1.0))
        clean_kind = str(kind or "fact").strip().lower()[:32] or "fact"
        safe_importance = max(0.0, min(float(importance), 1.0))
        now = self._now()

        with self._connect() as conn:
            self._ensure_session(conn, clean_session, now)
            # 新名字会取代旧名字；同一物品的喜欢/不喜欢互斥，保留旧记录但标为 inactive。
            if clean_predicate == "name":
                conn.execute(
                    "update memory_facts set active = 0, updated_at = ? where session_id = ? and subject = ? and predicate = 'name' and value <> ? and active = 1",
                    (now, clean_session, clean_subject, clean_value),
                )
            elif clean_predicate in {"likes", "dislikes"}:
                opposite = "dislikes" if clean_predicate == "likes" else "likes"
                conn.execute(
                    "update memory_facts set active = 0, updated_at = ? where session_id = ? and subject = ? and predicate = ? and value = ? and active = 1",
                    (now, clean_session, clean_subject, opposite, clean_value),
                )
            conn.execute(
                """
                insert into memory_facts(
                    session_id, subject, predicate, value, confidence,
                    source_turn_id, active, kind, importance, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                on conflict(session_id, subject, predicate, value)
                do update set
                    confidence = excluded.confidence,
                    source_turn_id = excluded.source_turn_id,
                    kind = excluded.kind,
                    importance = excluded.importance,
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
                    clean_kind,
                    safe_importance,
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

    def create_navigation_task(
        self,
        session_id: str,
        *,
        task_id: str,
        goal: str,
        command: str,
        target_ref: str,
    ) -> NavigationTask:
        """保存一条等待 Godot 导航结果的任务；新动作会取代同存档未完成的旧动作。"""
        clean_session = self._clean_session_id(session_id)
        clean_task_id = str(task_id or "").strip()
        clean_command = str(command or "").strip()
        clean_target = str(target_ref or "").strip()
        if not clean_task_id or not clean_command or not clean_target:
            raise ValueError("navigation task requires task_id, command and target_ref")
        now = self._now()
        with self._connect() as conn:
            self._ensure_session(conn, clean_session, now)
            conn.execute(
                "update navigation_tasks set status = 'cancelled', updated_at = ? where session_id = ? and status = 'waiting'",
                (now, clean_session),
            )
            conn.execute(
                """
                insert into navigation_tasks(
                    task_id, session_id, goal, command, target_ref, status,
                    last_event, last_result_json, created_at, updated_at
                ) values (?, ?, ?, ?, ?, 'waiting', '', '{}', ?, ?)
                """,
                (clean_task_id, clean_session, str(goal or "").strip()[:500], clean_command, clean_target, now, now),
            )
            row = conn.execute("select * from navigation_tasks where task_id = ?", (clean_task_id,)).fetchone()
        return self._task_from_row(row)

    def record_navigation_task_result(
        self,
        session_id: str,
        task_id: str,
        *,
        event: str,
        ok: bool,
        target_ref: str,
    ) -> NavigationTask | None:
        """以 Godot 回传的结果结束匹配的导航任务，模型文字不能改变这个结论。"""
        clean_session = self._clean_session_id(session_id)
        clean_task_id = str(task_id or "").strip()
        if not clean_task_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "select * from navigation_tasks where session_id = ? and task_id = ?",
                (clean_session, clean_task_id),
            ).fetchone()
            if row is None:
                return None
            task = self._task_from_row(row)
            actual_target = str(target_ref or "").strip()
            if task.status != "waiting" or (actual_target and actual_target != task.target_ref):
                return task
            clean_event = str(event or "").strip()
            # 现在任务表同时记录导航、取物和玩家接受等 Godot 任务；成功与否以
            # Godot 的布尔结果为准，而不是把事件名硬编码成某一种导航事件。
            status = "succeeded" if bool(ok) else "failed"
            now = self._now()
            result = {"ok": bool(ok), "target_ref": actual_target or task.target_ref}
            conn.execute(
                """
                update navigation_tasks
                set status = ?, last_event = ?, last_result_json = ?, updated_at = ?
                where task_id = ?
                """,
                (status, clean_event, self._json_dumps(result), now, clean_task_id),
            )
            updated = conn.execute("select * from navigation_tasks where task_id = ?", (clean_task_id,)).fetchone()
        return self._task_from_row(updated)

    def add_story_event(
        self,
        session_id: str,
        kind: str,
        summary: str,
        *,
        importance: float = 0.5,
        source_turn_id: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """记录可供后续 GM 接续的剧情事件，避免与玩家偏好混淆。"""
        clean_session = self._clean_session_id(session_id)
        clean_summary = str(summary or "").strip()
        if not clean_summary:
            raise ValueError("story event summary must not be empty")
        now = self._now()
        with self._connect() as conn:
            self._ensure_session(conn, clean_session, now)
            cursor = conn.execute(
                "insert into story_events(session_id, kind, summary, importance, source_turn_id, metadata_json, created_at) values (?, ?, ?, ?, ?, ?, ?)",
                (
                    clean_session,
                    str(kind or "event").strip()[:32] or "event",
                    clean_summary[:500],
                    max(0.0, min(float(importance), 1.0)),
                    int(source_turn_id),
                    self._json_dumps(metadata or {}),
                    now,
                ),
            )
            row = conn.execute("select * from story_events where id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def get_story_events(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "select * from story_events where session_id = ? order by id desc limit ?",
                (self._clean_session_id(session_id), max(1, min(int(limit), 100))),
            ).fetchall()
        return [self._story_event_to_dict(row) for row in rows]

    def supersede_story_events(
        self,
        session_id: str,
        continuity_key: str,
        *,
        status: str = "superseded",
    ) -> int:
        """关闭同一 continuity_key 的旧 active 标记，保留历史但避免下次重复续写。"""
        clean_session = self._clean_session_id(session_id)
        clean_key = str(continuity_key or "").strip()
        if not clean_key:
            return 0
        changed = 0
        now = self._now()
        with self._connect() as conn:
            rows = conn.execute(
                "select id, metadata_json from story_events where session_id = ?",
                (clean_session,),
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(str(row["metadata_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                if not isinstance(metadata, dict) or metadata.get("continuity_key") != clean_key:
                    continue
                if str(metadata.get("status", "active")) in {"resolved", "closed", "superseded"}:
                    continue
                metadata["status"] = str(status or "superseded")[:24]
                conn.execute(
                    "update story_events set metadata_json = ? where id = ?",
                    (self._json_dumps(metadata), int(row["id"])),
                )
                changed += 1
            if changed:
                conn.execute("update sessions set updated_at = ? where session_id = ?", (now, clean_session))
        return changed

    def _story_event_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """把 SQLite 的 JSON 元数据还原为字典，旧存档没有该字段也能读取。"""
        result = dict(row)
        raw = result.get("metadata_json", "{}")
        try:
            metadata = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        result["metadata"] = metadata if isinstance(metadata, dict) else {}
        return result

    def get_memory_facts_by_ids(self, session_id: str, fact_ids: list[int] | set[int] | tuple[int, ...]) -> list[MemoryFact]:
        clean_session = self._clean_session_id(session_id)
        ids: list[int] = []
        seen: set[int] = set()
        for raw_id in fact_ids:
            try:
                fact_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if fact_id <= 0 or fact_id in seen:
                continue
            ids.append(fact_id)
            seen.add(fact_id)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select * from memory_facts
                where session_id = ? and active = 1 and id in ({placeholders})
                """,
                (clean_session, *ids),
            ).fetchall()
        by_id = {int(row["id"]): self._fact_from_row(row) for row in rows}
        return [by_id[fact_id] for fact_id in ids if fact_id in by_id]

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
        copied_story_events = 0
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
                        source_turn_id, active, kind, importance, created_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    on conflict(session_id, subject, predicate, value)
                    do update set
                        confidence = excluded.confidence,
                        source_turn_id = excluded.source_turn_id,
                        kind = excluded.kind,
                        importance = excluded.importance,
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
                        str(row["kind"]),
                        float(row["importance"]),
                        str(row["created_at"]),
                        str(row["updated_at"]),
                    ),
                )
                copied_facts += 1
            event_rows = conn.execute(
                """
                select * from story_events
                where session_id = ? and (source_turn_id = 0 or source_turn_id <= ?)
                order by id asc
                """,
                (source_session, safe_checkpoint),
            ).fetchall()
            for row in event_rows:
                source_turn_id = int(row["source_turn_id"])
                mapped_turn_id = int(turn_id_map.get(source_turn_id, 0 if source_turn_id <= 0 else source_turn_id))
                conn.execute(
                    """
                    insert into story_events(session_id, kind, summary, importance, source_turn_id, metadata_json, created_at)
                    values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_session,
                        str(row["kind"]),
                        str(row["summary"]),
                        float(row["importance"]),
                        mapped_turn_id,
                        str(row["metadata_json"] or "{}") if "metadata_json" in row.keys() else "{}",
                        str(row["created_at"]),
                    ),
                )
                copied_story_events += 1
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
            "story_events_copied": copied_story_events,
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
            "story_events": self.get_story_events(clean_session, recent_limit),
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
            events_deleted = conn.execute(
                "delete from story_events where session_id = ?",
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
            "story_events_deleted": max(0, int(events_deleted)),
        }

    def clear_all(self) -> dict[str, Any]:
        with self._connect() as conn:
            turns_deleted = conn.execute("delete from turns").rowcount
            facts_deleted = conn.execute("delete from memory_facts").rowcount
            events_deleted = conn.execute("delete from story_events").rowcount
            conn.execute("delete from sessions")
        return {
            "ok": True,
            "clear_all": True,
            "turns_deleted": max(0, int(turns_deleted)),
            "facts_deleted": max(0, int(facts_deleted)),
            "story_events_deleted": max(0, int(events_deleted)),
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

    def _task_from_row(self, row: sqlite3.Row) -> NavigationTask:
        return NavigationTask(
            task_id=str(row["task_id"]),
            session_id=str(row["session_id"]),
            goal=str(row["goal"]),
            command=str(row["command"]),
            target_ref=str(row["target_ref"]),
            status=str(row["status"]),
            last_event=str(row["last_event"]),
            last_result=self._json_loads(str(row["last_result_json"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
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
            kind=str(row["kind"]),
            importance=float(row["importance"]),
        )

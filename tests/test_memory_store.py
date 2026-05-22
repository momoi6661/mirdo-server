from pathlib import Path

from app.memory.store import MemoryStore


def test_memory_store_initializes_schema(tmp_path: Path):
    db_path = tmp_path / "memory.sqlite3"
    store = MemoryStore(db_path)
    store.initialize()

    assert db_path.exists()
    assert store.health() is True


def test_memory_store_adds_and_reads_turns(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()

    user_turn = store.add_turn("session-a", "user", "你好", {"source": "test"})
    assistant_turn = store.add_turn("session-a", "assistant", "你好，老师。", {"ok": True})

    assert user_turn.id > 0
    assert assistant_turn.id == user_turn.id + 1

    turns = store.get_recent_turns("session-a", limit=10)
    assert [turn.role for turn in turns] == ["user", "assistant"]
    assert turns[0].content == "你好"
    assert turns[0].payload["source"] == "test"


def test_memory_store_upserts_facts_and_snapshot(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    turn = store.add_turn("session-a", "user", "记住我喜欢罐头汤", {})

    first = store.upsert_memory_fact(
        "session-a",
        subject="player",
        predicate="likes",
        value="罐头汤",
        confidence=0.7,
        source_turn_id=turn.id,
    )
    second = store.upsert_memory_fact(
        "session-a",
        subject="player",
        predicate="likes",
        value="罐头汤",
        confidence=0.9,
        source_turn_id=turn.id,
    )

    assert first.id == second.id
    assert second.confidence == 0.9

    snapshot = store.get_session_snapshot("session-a", recent_limit=5)
    assert snapshot["session_id"] == "session-a"
    assert len(snapshot["recent_turns"]) == 1
    assert snapshot["memory_facts"][0]["value"] == "罐头汤"


def test_memory_store_clear_session_and_clear_all(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    store.add_turn("session-a", "user", "a", {})
    store.add_turn("session-b", "user", "b", {})
    store.upsert_memory_fact("session-a", "player", "likes", "水", 0.8, 1)

    cleared = store.clear_session("session-a")
    assert cleared["turns_deleted"] == 1
    assert cleared["facts_deleted"] == 1
    assert len(store.get_recent_turns("session-a", 10)) == 0
    assert len(store.get_recent_turns("session-b", 10)) == 1

    all_cleared = store.clear_all()
    assert all_cleared["turns_deleted"] == 1
    assert len(store.get_recent_turns("session-b", 10)) == 0


def test_memory_store_search_ranks_relevant_facts(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    store.upsert_memory_fact("session-a", "player", "likes", "罐头汤", 0.9, 0)
    store.upsert_memory_fact("session-a", "player", "dislikes", "噪声", 0.8, 0)
    store.upsert_memory_fact("session-a", "player", "note", "答应Mirdo会安全回家", 0.75, 0)

    facts = store.search_memory_facts("session-a", "老师问你还记得我喜欢吃什么吗？", limit=2)

    assert facts
    assert facts[0].value == "罐头汤"
    assert len(facts) == 2


def test_memory_store_lists_sessions_and_deletes_fact(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    turn = store.add_turn("session-a", "user", "你好", {})
    store.add_turn("session-b", "user", "另一个存档", {})
    fact = store.upsert_memory_fact("session-a", "player", "likes", "罐头汤", 0.9, turn.id)

    sessions = store.list_sessions(limit=10)
    session_ids = {item["session_id"] for item in sessions}
    assert {"session-a", "session-b"}.issubset(session_ids)
    session_a = next(item for item in sessions if item["session_id"] == "session-a")
    assert session_a["turn_count"] == 1
    assert session_a["memory_fact_count"] == 1

    deleted = store.delete_memory_fact("session-a", fact.id)
    assert deleted["ok"] is True
    assert deleted["deleted"] is True
    assert store.get_memory_facts("session-a") == []
    assert store.delete_memory_fact("session-a", fact.id)["deleted"] is False


def test_memory_store_forks_session_at_checkpoint(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    first_user = store.add_turn("main", "user", "旧问题", {})
    first_assistant = store.add_turn("main", "assistant", "旧回答", {})
    store.upsert_memory_fact("main", "player", "likes", "罐头汤", 0.9, first_user.id)
    future_user = store.add_turn("main", "user", "未来问题", {})
    store.add_turn("main", "assistant", "未来回答", {})
    store.upsert_memory_fact("main", "player", "likes", "清水", 0.9, future_user.id)

    result = store.fork_session("main", first_assistant.id, "branch")

    assert result["turns_copied"] == 2
    assert result["facts_copied"] == 1
    history = store.get_session_history("branch", 10)["turns"]
    assert [turn["content"] for turn in history] == ["旧问题", "旧回答"]
    facts = store.get_memory_facts("branch")
    assert [fact.value for fact in facts] == ["罐头汤"]
    assert store.get_latest_turn_id("main") > first_assistant.id
    assert store.get_latest_turn_id("branch") > 0


def test_memory_store_gets_active_facts_by_ids(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    first = store.upsert_memory_fact("session-a", "player", "likes", "清水", 0.9, 0)
    second = store.upsert_memory_fact("session-a", "player", "dislikes", "噪声", 0.8, 0)
    other_session = store.upsert_memory_fact("session-b", "player", "likes", "罐头汤", 0.7, 0)
    store.delete_memory_fact("session-a", second.id)

    facts = store.get_memory_facts_by_ids("session-a", [first.id, second.id, other_session.id, 999999])

    assert [fact.id for fact in facts] == [first.id]

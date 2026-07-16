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



def test_memory_store_reconciles_navigation_task_from_godot_result(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()

    waiting = store.create_navigation_task(
        "session-a",
        task_id="task-toilet",
        goal="去卫生间看看",
        command="go_to_nav_point",
        target_ref="toilet_look_point",
    )
    succeeded = store.record_navigation_task_result(
        "session-a",
        "task-toilet",
        event="navigation_goal_finished",
        ok=True,
        target_ref="toilet_look_point",
    )
    store.create_navigation_task(
        "session-a",
        task_id="task-mirror",
        goal="去看镜子",
        command="go_to_object",
        target_ref="bathroom_mirror",
    )
    failed = store.record_navigation_task_result(
        "session-a",
        "task-mirror",
        event="navigation_goal_failed",
        ok=False,
        target_ref="bathroom_mirror",
    )

    assert waiting.status == "waiting"
    assert succeeded is not None and succeeded.status == "succeeded"
    assert succeeded.last_event == "navigation_goal_finished"
    assert failed is not None and failed.status == "failed"


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
    store.add_story_event("main", "daily_life", "老师和 Mirdo 一起修好了收音机", source_turn_id=first_assistant.id)
    future_user = store.add_turn("main", "user", "未来问题", {})
    store.add_turn("main", "assistant", "未来回答", {})
    store.upsert_memory_fact("main", "player", "likes", "清水", 0.9, future_user.id)

    result = store.fork_session("main", first_assistant.id, "branch")

    assert result["turns_copied"] == 2
    assert result["facts_copied"] == 1
    assert result["story_events_copied"] == 1
    history = store.get_session_history("branch", 10)["turns"]
    assert [turn["content"] for turn in history] == ["旧问题", "旧回答"]
    facts = store.get_memory_facts("branch")
    assert [fact.value for fact in facts] == ["罐头汤"]
    assert store.get_story_events("branch")[0]["summary"] == "老师和 Mirdo 一起修好了收音机"
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


def test_memory_store_separates_fact_kind_and_story_event(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    fact = store.upsert_memory_fact("s1", "player", "promised", "外出前带绷带", kind="promise", importance=0.9)
    event = store.add_story_event("s1", "observation", "入口有不明撞击声", importance=0.8)

    assert fact.kind == "promise"
    assert fact.importance == 0.9
    assert event["kind"] == "observation"
    assert store.get_story_events("s1")[0]["summary"] == "入口有不明撞击声"


def test_memory_store_keeps_story_marker_metadata_and_supersedes_old_version(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    store.add_story_event(
        "s1",
        "expedition",
        "旧药店后门仍然锁着",
        metadata={"continuity_key": "old-pharmacy", "location_id": "pharmacy", "status": "active"},
    )
    store.supersede_story_events("s1", "old-pharmacy")
    events = store.get_story_events("s1")

    assert events[0]["metadata"]["continuity_key"] == "old-pharmacy"
    assert events[0]["metadata"]["status"] == "superseded"


def test_memory_store_replaces_conflicting_identity_and_preference(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    store.upsert_memory_fact("s1", "player", "name", "小李")
    store.upsert_memory_fact("s1", "player", "name", "老师")
    store.upsert_memory_fact("s1", "player", "likes", "热可可")
    store.upsert_memory_fact("s1", "player", "dislikes", "热可可")

    facts = {(fact.predicate, fact.value) for fact in store.get_memory_facts("s1")}

    assert ("name", "老师") in facts
    assert ("name", "小李") not in facts
    assert ("dislikes", "热可可") in facts
    assert ("likes", "热可可") not in facts


def test_memory_store_updates_and_reads_session_summary(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    first = store.add_turn("s1", "user", "老师，我喜欢热可可。", {})
    store.add_turn("s1", "assistant", "我记住啦。", {})

    store.update_session_summary("s1", "老师喜欢热可可，Mirdo 已答应记住。", first.id)
    summary, summary_turn_id = store.get_session_summary("s1")

    assert "热可可" in summary
    assert summary_turn_id == first.id
    assert [turn.role for turn in store.get_turns_after("s1", first.id)] == ["assistant"]

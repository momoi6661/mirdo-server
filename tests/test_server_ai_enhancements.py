from pathlib import Path

from app.chat_orchestrator import ChatOrchestrator
from app.config import Settings
from app.llm_provider import LLMProvider
from app.memory.store import MemoryStore
from app.schemas import ChatRequest


def test_chat_orchestrator_persists_player_memory_and_exposes_debug(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    fake_model = _FakeChatModel(
        '{"dialogue":"我记住了，老师。","emotion":"温和","action":"Talk",'
        '"memory_updates":[{"subject":"player","predicate":"likes","value":"清水","confidence":0.8}]}'
    )
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: fake_model)
    orchestrator = ChatOrchestrator(settings=settings, memory_store=store, llm_provider=llm_provider)

    response = orchestrator.chat(ChatRequest(session_id="s1", player_text="记住我叫刘队，我喜欢罐头汤。"))

    snapshot = store.get_session_snapshot("s1")
    facts = {(fact["subject"], fact["predicate"], fact["value"]) for fact in snapshot["memory_facts"]}
    assert ("player", "name", "刘队") in facts
    assert ("player", "likes", "罐头汤") in facts
    assert ("player", "likes", "清水") in facts
    assert response.memory_updates
    assert any(item["value"] == "罐头汤" for item in response.memory_updates)


def test_next_chat_prompt_includes_persisted_memory(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    store.upsert_memory_fact("s1", "player", "likes", "罐头汤", 0.9, 0)
    fake_model = _FakeChatModel('{"dialogue":"我记得，你喜欢罐头汤。","emotion":"温和","action":"Talk"}')
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: fake_model)
    orchestrator = ChatOrchestrator(settings=settings, memory_store=store, llm_provider=llm_provider)

    orchestrator.chat(ChatRequest(session_id="s1", player_text="你记得我喜欢什么吗？"))

    flattened = "\n".join(content for _role, content in fake_model.last_messages)
    assert "player likes: 罐头汤" in flattened


def test_chat_orchestrator_local_fallback_when_model_fails(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()

    def broken_model(_resolved):
        raise RuntimeError("boom secret-key")

    llm_provider = LLMProvider(settings, chat_model_factory=broken_model)
    orchestrator = ChatOrchestrator(settings=settings, memory_store=store, llm_provider=llm_provider)

    response = orchestrator.chat(ChatRequest(session_id="s1", player_text="你好呀", npc_stats={"mood": 20}))

    assert response.ok is True
    assert response.fallback is True
    assert response.error == "model_call_failed"
    assert "信号" in response.dialogue or "老师" in response.dialogue
    assert "队长" not in response.dialogue


def test_prompt_builder_contains_richer_role_contract(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    fake_model = _FakeChatModel('{"dialogue":"收到。","emotion":"警觉","action":"Talk"}')
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: fake_model)
    orchestrator = ChatOrchestrator(settings=settings, memory_store=store, llm_provider=llm_provider)

    orchestrator.chat(ChatRequest(session_id="s1", player_text="外面安全吗？", npc_stats={"hunger": 18, "thirst": 22}))

    flattened = "\n".join(content for _role, content in fake_model.last_messages)
    assert "Mirdo" in flattened
    assert "VRChat" in flattened
    assert "不要自称小空" in flattened
    assert "绝对不要叫玩家“队长”" in flattened
    assert "动作只能从" in flattened
    assert "memory_updates" in flattened
    assert "数值越低越需要食物" in flattened


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="https://example.test/v1",
        api_key="secret-key",
        chat_model="model-a",
    )


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChatModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.last_messages = None

    def invoke(self, messages):
        self.last_messages = messages
        return _FakeMessage(self.content)


def test_chat_prompt_uses_relevant_memory_not_only_newest(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    store.upsert_memory_fact("s1", "player", "likes", "罐头汤", 0.9, 0)
    for index in range(18):
        store.upsert_memory_fact("s1", "player", "note", f"普通记录{index}", 0.6, 0)
    fake_model = _FakeChatModel('{"dialogue":"我记得，老师喜欢罐头汤。","emotion":"温和","action":"Talk"}')
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: fake_model)
    orchestrator = ChatOrchestrator(settings=settings, memory_store=store, llm_provider=llm_provider)

    response = orchestrator.chat(ChatRequest(session_id="s1", player_text="你还记得我喜欢吃什么吗？"))

    flattened = "\n".join(content for _role, content in fake_model.last_messages)
    assert response.used_memory
    assert "player likes: 罐头汤" in flattened


def test_chat_orchestrator_prefers_memory_rag_retriever(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    fake_model = _FakeChatModel('{"dialogue":"我记得，老师喜欢罐头汤。","emotion":"温和","action":"Talk"}')
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: fake_model)
    memory_retriever = _FakeMemoryRetriever([{"subject":"player","predicate":"likes","value":"罐头汤"}])
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=llm_provider,
        memory_retriever=memory_retriever,
    )

    response = orchestrator.chat(ChatRequest(session_id="s1", player_text="你记得我喜欢什么吗？"))

    assert memory_retriever.last_query == "你记得我喜欢什么吗？"
    assert response.used_memory == memory_retriever.hits
    assert "player likes: 罐头汤" in "\n".join(content for _role, content in fake_model.last_messages)


class _FakeMemoryRetriever:
    def __init__(self, hits):
        self.hits = hits
        self.last_session_id = ""
        self.last_query = ""
        self.last_top_k = 0

    def retrieve(self, session_id: str, query: str, top_k: int = 12):
        self.last_session_id = session_id
        self.last_query = query
        self.last_top_k = top_k
        return self.hits


class _FakeRagRetriever:
    def __init__(self, hits):
        self.hits = hits
        self.last_query = ""
        self.last_top_k = 0

    def retrieve(self, query: str, top_k: int = 4):
        self.last_query = query
        self.last_top_k = top_k
        return self.hits


def test_chat_from_old_checkpoint_forks_parallel_timeline(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    first_model = _FakeChatModel('{"dialogue":"我记住了，老师。","emotion":"温和","action":"Talk"}')
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: first_model)
    orchestrator = ChatOrchestrator(settings=settings, memory_store=store, llm_provider=llm_provider)

    first = orchestrator.chat(ChatRequest(session_id="mirdo:slot_01", player_text="记住我喜欢罐头汤。"))
    old_checkpoint = first.turn_id
    second = orchestrator.chat(ChatRequest(session_id="mirdo:slot_01", player_text="后来我喜欢清水。"))
    assert second.turn_id > old_checkpoint

    branch_model = _FakeChatModel('{"dialogue":"这是新的分支。","emotion":"温和","action":"Talk"}')
    orchestrator.llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: branch_model)
    forked = orchestrator.chat(ChatRequest(
        session_id="mirdo:slot_01",
        player_text="从旧进度继续。",
        context={"ai_checkpoint_turn_id": old_checkpoint, "save_slot": "slot_01"},
    ))

    assert forked.session_id != "mirdo:slot_01"
    assert forked.session_id.startswith("mirdo:slot_01:branch_")
    assert forked.forked_from == "mirdo:slot_01"
    assert forked.forked_at_turn_id == old_checkpoint
    assert forked.turn_id > 0

    branch_history = store.get_session_history(forked.session_id, limit=20)["turns"]
    assert [turn["role"] for turn in branch_history] == ["user", "assistant", "user", "assistant"]
    assert branch_history[0]["content"] == "记住我喜欢罐头汤。"
    assert branch_history[2]["content"] == "从旧进度继续。"
    assert all("后来我喜欢清水" not in turn["content"] for turn in branch_history)


def test_chat_at_latest_checkpoint_keeps_same_timeline(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    fake_model = _FakeChatModel('{"dialogue":"好的。","emotion":"温和","action":"Talk"}')
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: fake_model)
    orchestrator = ChatOrchestrator(settings=settings, memory_store=store, llm_provider=llm_provider)

    first = orchestrator.chat(ChatRequest(session_id="mirdo:slot_01", player_text="第一句。"))
    second = orchestrator.chat(ChatRequest(
        session_id="mirdo:slot_01",
        player_text="第二句。",
        context={"ai_checkpoint_turn_id": first.turn_id},
    ))

    assert second.session_id == "mirdo:slot_01"
    assert not hasattr(second, "forked_from") or second.forked_from == ""


def test_agent_style_ordered_messages_use_clean_queries_and_final_intent(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    fake_model = _FakeChatModel('{"dialogue":"好呀老师，我陪你看入口。","emotion":"温和","action":"listen"}')
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: fake_model)
    memory_retriever = _FakeMemoryRetriever([])
    rag_retriever = _FakeRagRetriever([])
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=llm_provider,
        memory_retriever=memory_retriever,
        rag_retriever=rag_retriever,
    )
    player_text = "\n".join(
        [
            "玩家连续输入了几句话，请像 AI Agent 处理连续用户消息一样按时间顺序理解：",
            "后续内容可能是补充、修正、打断、强调或新目标；不要机械逐句回答，综合判断玩家当前最终意图后自然回应。",
            "第1句：你先别去食物柜。",
            "随后：刚才门口好像有声音。",
            "继续：先陪我看一下入口。",
        ]
    )

    response = orchestrator.chat(
        ChatRequest(
            session_id="s1",
            player_text=player_text,
            context={
                "ai_nav_points": [
                    {"id": "food_cabinet", "name": "食物柜", "tags": ["food"], "action_options": ["work_count_supplies"]},
                    {"id": "entrance", "name": "入口", "tags": ["door", "entrance"], "action_options": ["look_around"]},
                ]
            },
        )
    )

    assert "玩家连续输入" not in memory_retriever.last_query
    assert "玩家连续输入" not in rag_retriever.last_query
    flattened = "\n".join(content for _role, content in fake_model.last_messages)
    assert "连续玩家输入处理规则" in flattened
    assert response.command != "go_to_nav_point" or response.command_payload.get("target_nav_point") != "food_cabinet"


def test_agent_style_ordered_messages_memory_extraction_uses_latest_correction(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    fake_model = _FakeChatModel('{"dialogue":"我记住了，老师。","emotion":"温和","action":"listen"}')
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: fake_model)
    orchestrator = ChatOrchestrator(settings=settings, memory_store=store, llm_provider=llm_provider)
    player_text = "\n".join(
        [
            "玩家连续输入了几句话，请像 AI Agent 处理连续用户消息一样按时间顺序理解：",
            "第1句：记住我喜欢罐头汤。",
            "随后：不对，记住我喜欢清水。",
        ]
    )

    orchestrator.chat(ChatRequest(session_id="s1", player_text=player_text))

    snapshot = store.get_session_snapshot("s1")
    facts = {(fact["predicate"], fact["value"]) for fact in snapshot["memory_facts"]}
    assert ("likes", "清水") in facts
    assert ("likes", "罐头汤") not in facts

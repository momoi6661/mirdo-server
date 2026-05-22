from pathlib import Path

from app.chat_orchestrator import ChatOrchestrator
from app.config import Settings
from app.llm_provider import LLMProvider
from app.memory.store import MemoryStore
from app.schemas import ChatRequest


def test_chat_orchestrator_invokes_model_parses_response_and_writes_turns(tmp_path: Path):
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="http://localhost:11434/v1",
        api_key="",
        chat_model="qwen3",
    )
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: _FakeChatModel(
        '{"dialogue":"你好，老师。","emotion":"平静","action":"Talk","stat_change":{"mood":1},"memory_tags":["greeting"]}'
    ))
    orchestrator = ChatOrchestrator(settings=settings, memory_store=store, llm_provider=llm_provider)

    response = orchestrator.chat(ChatRequest(session_id="s1", player_text="你好"))

    assert response.ok is True
    assert response.dialogue == "你好，老师。"
    assert response.session_id == "s1"
    assert response.turn_id > 0

    turns = store.get_recent_turns("s1", limit=10)
    assert [turn.role for turn in turns] == ["user", "assistant"]
    assert turns[0].content == "你好"
    assert turns[1].content == "你好，老师。"


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



def test_chat_orchestrator_injects_rag_hits_and_returns_used_knowledge(tmp_path: Path):
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="http://localhost:11434/v1",
        api_key="",
        chat_model="qwen3",
        top_k=2,
    )
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    fake_model = _FakeChatModel('{"dialogue":"我会把罐头汤留给你。","emotion":"温和","action":"Talk"}')
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: fake_model)
    retriever = _FakeRetriever([
        {"text": "小空会记得玩家喜欢罐头汤。", "source": "xiaokong_persona.md", "category": "persona"}
    ])
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=llm_provider,
        rag_retriever=retriever,
    )

    response = orchestrator.chat(ChatRequest(session_id="s1", player_text="你记得我喜欢什么吗？"))

    assert retriever.last_query == "你记得我喜欢什么吗？"
    assert response.used_knowledge == retriever.hits
    assert any("罐头汤" in message[1] for message in fake_model.last_messages)


class _FakeRetriever:
    def __init__(self, hits):
        self.hits = hits
        self.last_query = None
        self.last_top_k = None

    def retrieve(self, query: str, top_k: int = 4):
        self.last_query = query
        self.last_top_k = top_k
        return self.hits


def test_chat_orchestrator_uses_json_mode_for_complete_response(tmp_path: Path):
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="http://localhost:11434/v1",
        api_key="",
        chat_model="qwen3",
    )
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    calls = []

    class CaptureProvider:
        def build_chat_model(self, request_provider=None, *, max_tokens=None, timeout=None, json_mode=False):
            calls.append({"json_mode": json_mode, "max_tokens": max_tokens, "timeout": timeout})
            return _FakeChatModel('{"dialogue":"收到，老师。","emotion":"温和","action":"Talk"}')

    orchestrator = ChatOrchestrator(settings=settings, memory_store=store, llm_provider=CaptureProvider())

    response = orchestrator.chat(ChatRequest(session_id="s1", player_text="你好"))

    assert response.dialogue == "收到，老师。"
    assert calls and calls[0]["json_mode"] is True


class _SequenceFakeChatModel:
    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.last_messages = None
        self.all_messages = []

    def invoke(self, messages):
        self.last_messages = messages
        self.all_messages.append(messages)
        content = self.contents.pop(0) if self.contents else '{"dialogue":"嗯，老师。","action":"Talk"}'
        return _FakeMessage(content)


def test_chat_orchestrator_carries_task_chain_command_payload_into_next_prompt(tmp_path: Path):
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="http://localhost:11434/v1",
        api_key="",
        chat_model="qwen3",
    )
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    fake_model = _SequenceFakeChatModel(
        [
            '{"dialogue":"好呀老师，我去看看镜子。","emotion":"认真","expression":"neutral","action":"walk","command":"go_to_nav_point","command_payload":{"target_nav_point":"bathroom_mirror_look","chain_id":"mirror_chain","chain_depth":1}}',
            '{"dialogue":"老师，镜子这边我看过啦，暂时没发现奇怪的东西。","emotion":"认真","expression":"neutral","action":"cute_explain"}',
        ]
    )
    llm_provider = LLMProvider(settings, chat_model_factory=lambda _resolved: fake_model)
    orchestrator = ChatOrchestrator(settings=settings, memory_store=store, llm_provider=llm_provider)

    nav_context = {
        "known_nav_points": [
            {
                "id": "bathroom_mirror_look",
                "name": "卫生间镜子检查点",
                "type": "bathroom",
                "description": "可以观察镜子和洗手台。",
            }
        ]
    }
    first = orchestrator.chat(ChatRequest(session_id="s1", player_text="去厕所看看镜子里面有什么。", context=nav_context))
    assert first.command == "go_to_nav_point"
    assert first.command_payload["chain_id"] == "mirror_chain"

    follow_up_context = dict(nav_context)
    follow_up_context["source_decision"] = {
        "kind": "external_goal_follow_up",
        "event": "navigation_goal_finished",
        "target_nav_point": "bathroom_mirror_look",
        "target_name": "卫生间镜子",
        "chain_id": "mirror_chain",
        "chain_depth": 2,
    }
    second = orchestrator.chat(
        ChatRequest(
            session_id="s1",
            player_text="Mirdo 已经到达卫生间镜子，请反馈结果并判断是否继续。",
            context=follow_up_context,
        )
    )

    assert second.command == ""
    flattened_second_prompt = "\n".join(content for _role, content in fake_model.all_messages[-1])
    assert "recent_dialogue" in flattened_second_prompt
    assert "chain_id=mirror_chain depth=1 command=go_to_nav_point target=bathroom_mirror_look" in flattened_second_prompt
    assert "source_decision=kind=external_goal_follow_up" in flattened_second_prompt

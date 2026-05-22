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

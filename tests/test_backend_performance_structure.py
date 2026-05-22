import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.llm_provider import LLMProvider
from app.main import create_app
from app.rag.retriever import RAGRetriever


def test_rag_retriever_reuses_vector_store_between_retrievals(tmp_path: Path):
    retriever = RAGRetriever(tmp_path / "chroma")
    calls = []

    class FakeStore:
        def similarity_search(self, query: str, k: int):
            calls.append((query, k))
            return []

    retriever._ready = lambda: True
    retriever._store_factory = lambda: FakeStore()

    retriever.retrieve("食物柜", top_k=2)
    retriever.retrieve("医疗柜", top_k=3)

    assert len(calls) == 2
    assert retriever._store_factory_calls == 1


def test_llm_provider_reuses_default_chat_model_for_same_options(tmp_path: Path):
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="http://localhost:11434/v1",
        api_key="",
        chat_model="qwen3",
    )
    created = []

    def factory(_resolved):
        model = object()
        created.append(model)
        return model

    provider = LLMProvider(settings, chat_model_factory=factory)

    assert provider.build_chat_model() is provider.build_chat_model()
    assert len(created) == 1


def test_chat_and_outing_routes_do_not_block_each_other(tmp_path: Path):
    settings = Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="http://localhost:11434/v1",
        api_key="",
        chat_model="qwen3",
    )
    app = create_app(settings, chat_model_factory=lambda _resolved: _SlowFakeChatModel())

    outing_payload = {
        "session_id": "outing-test",
        "location": {"id": "clinic", "name": "街区诊所"},
        "available_loot": {"default": ["res://resources/items/bandage.tres"]},
    }

    with TestClient(app) as client:
        with client.websocket_connect if False else _Noop():
            # Use TestClient's thread-safe request helpers from two Python threads.
            import threading

            chat_status = {}
            outing_status = {}

            chat_thread = threading.Thread(
                target=lambda: chat_status.update(
                    client.post("/chat", json={"session_id": "s1", "player_text": "你好"}).json()
                )
            )
            outing_thread = threading.Thread(
                target=lambda: outing_status.update(client.post("/outing/resolve", json=outing_payload).json())
            )

            started = time.perf_counter()
            chat_thread.start()
            outing_thread.start()
            chat_thread.join(timeout=2.0)
            outing_thread.join(timeout=2.0)
            elapsed = time.perf_counter() - started

    assert chat_status.get("dialogue") == "老师，收到。"
    assert outing_status.get("title") == "外出报告"
    assert elapsed < 0.9


class _Noop:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


class _SlowFakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _SlowFakeChatModel:
    def invoke(self, messages):
        time.sleep(0.45)
        if messages and "outing-test" in str(messages):
            return _SlowFakeMessage(
                '{"title":"外出报告","summary":"完成。","story":"搜索完成。",'
                '"experience":["进入诊所。"],"risk_result":"安全。",'
                '"loot":[{"item_path":"res://resources/items/bandage.tres"}]}'
            )
        return _SlowFakeMessage('{"dialogue":"收到。","emotion":"平静","action":"listen"}')

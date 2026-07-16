from __future__ import annotations

from pathlib import Path

from app.chat_orchestrator import ChatOrchestrator
from app.config import Settings
from app.llm_provider import LLMProvider
from app.memory.store import MemoryStore
from app.schemas import ChatRequest, ChatResponse


class TranslationAgent:
    async def run(self, _prompt: str, *, deps, **_kwargs):
        return type(
            "AgentRunResult",
            (),
            {"output": ChatResponse(dialogue="我有点累。", dialogue_ja="ちょっと疲れた。")},
        )()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversation.sqlite3",
        rag_db=tmp_path / "rag.sqlite3",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="http://localhost:11434/v1",
        chat_model="test-model",
    )


def test_agent_translation_field_is_controlled_by_request(tmp_path: Path) -> None:
    """模型即使多返回了日语，未请求时 Graph 也会清空它。"""

    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=LLMProvider(settings),
        agent_factory=lambda *_args: TranslationAgent(),
    )

    without_translation = orchestrator.chat(ChatRequest(session_id="zh", player_text="我累了"))
    with_translation = orchestrator.chat(
        ChatRequest(session_id="ja", player_text="我累了", generate_japanese=True)
    )

    assert without_translation.dialogue_ja == ""
    assert with_translation.dialogue_ja == "ちょっと疲れた。"

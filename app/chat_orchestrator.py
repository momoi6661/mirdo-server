"""Chat 的薄入口：依赖在这里组装，回合流程在 Pydantic Graph 中执行。"""
from __future__ import annotations

from typing import Any

from .agent_graphs import ChatGraphDeps, run_chat_graph, run_chat_graph_async
from .character_ai import GodotBehaviorValidator
from .config import Settings
from .llm_provider import LLMProvider
from .memory.extractor import MemoryExtractor
from .memory.store import MemoryStore
from .mirdo_agent import AgentFactory, AgentPool, build_mirdo_agent
from .prompt_builder import PromptBuilder
from .schemas import ChatRequest, ChatResponse


class ChatRequestCoordinator:
    """按 session 记录客户端最新请求，避免改口后的旧回合污染对话。"""

    def __init__(self) -> None:
        self._latest: dict[str, tuple[int, str]] = {}

    def register(self, request: ChatRequest) -> None:
        request_id = str(request.client_request_id).strip()
        sequence = int(request.client_sequence)
        if not request_id and sequence <= 0:
            return
        session_id = str(request.session_id).strip() or "default_session"
        previous = self._latest.get(session_id)
        if previous is None or sequence >= previous[0]:
            self._latest[session_id] = (sequence, request_id)

    def is_current(self, request: ChatRequest) -> bool:
        request_id = str(request.client_request_id).strip()
        sequence = int(request.client_sequence)
        if not request_id and sequence <= 0:
            return True
        latest = self._latest.get(str(request.session_id).strip() or "default_session")
        if latest is None:
            return True
        return latest == (sequence, request_id)


class ChatOrchestrator:
    """为 FastAPI 提供同步 chat 入口，不在这里重复实现流程。"""

    def __init__(
        self,
        *,
        settings: Settings,
        memory_store: MemoryStore,
        llm_provider: LLMProvider,
        prompt_builder: PromptBuilder | None = None,
        rag_retriever: Any | None = None,
        memory_retriever: Any | None = None,
        memory_extractor: MemoryExtractor | None = None,
        godot_behavior_validator: GodotBehaviorValidator | None = None,
        agent_factory: AgentFactory = build_mirdo_agent,
        agent_pool: AgentPool | None = None,
        request_coordinator: ChatRequestCoordinator | None = None,
    ) -> None:
        """保存 graph 所需的服务；测试可用 ``agent_factory`` 注入假 Agent。"""
        self.deps = ChatGraphDeps(
            settings=settings,
            memory_store=memory_store,
            llm_provider=llm_provider,
            agent_factory=agent_factory,
            agent_pool=agent_pool,
            prompt_builder=prompt_builder or PromptBuilder(),
            rag_retriever=rag_retriever,
            memory_retriever=memory_retriever,
            memory_extractor=memory_extractor or MemoryExtractor(),
            validator=godot_behavior_validator or GodotBehaviorValidator(),
            request_coordinator=request_coordinator,
        )

    def chat(self, request: ChatRequest) -> ChatResponse:
        """同步入口，仅供测试和本地脚本调用。"""
        return run_chat_graph(request=request, deps=self.deps)

    async def chat_async(self, request: ChatRequest) -> ChatResponse:
        """线上入口：在 FastAPI 事件循环运行图，以复用常驻模型连接。"""
        return await run_chat_graph_async(request=request, deps=self.deps)

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from pydantic_ai.exceptions import ModelHTTPError
from starlette.concurrency import run_in_threadpool

from .chat_orchestrator import ChatOrchestrator, ChatRequestCoordinator
from .config import Settings, get_settings
from .expedition_agent import build_expedition_agent
from .expedition_orchestrator import ExpeditionOrchestrator
from .llm_provider import LLMProvider
from .logging_setup import configure_file_logging
from .mirdo_agent import AgentFactory, AgentPool, build_mirdo_agent, build_probe_agent
from .memory.retriever import MemoryRAGRetriever
from .memory.store import MemoryStore
from .rag.indexer import RAGIndexer
from .rag.retriever import RAGRetriever
from .schemas import (
    ChatRequest,
    ExpeditionRequest,
    GodotActionResultRequest,
    IngestRequest,
    MemoryClearRequest,
    ProviderConfig,
)
from .tts.config import get_tts_settings
from .tts.chat import attach_tts_to_response
from .tts.routes import router as tts_router
from .tts.service import TTSService


def create_app(
    settings: Settings | None = None,
    agent_factory: AgentFactory = build_mirdo_agent,
    expedition_agent_factory: AgentFactory | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    # 生产环境用独立 GM；测试或集成方若显式传入一个替身，则继续让它同时覆盖两个入口。
    resolved_expedition_factory = expedition_agent_factory or (
        agent_factory if agent_factory is not build_mirdo_agent else build_expedition_agent
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_file_logging(resolved_settings.runtime_dir)
        resolved_settings.ensure_runtime_dirs()
        tts_settings = get_tts_settings()
        tts_settings.ensure_dirs()
        tts_service = TTSService(tts_settings) if tts_settings.enabled else None
        memory_store = MemoryStore(resolved_settings.conversation_db)
        memory_store.initialize()
        request_coordinator = ChatRequestCoordinator()
        llm_provider = LLMProvider(resolved_settings)
        rag_retriever = RAGRetriever(settings=resolved_settings)
        # 自定义测试 Agent 不一定是 PydanticAI Agent，因此只为默认生产 Agent 建立连接池。
        agent_pool = AgentPool() if agent_factory is build_mirdo_agent else None
        app.state.settings = resolved_settings
        app.state.tts_settings = tts_settings
        app.state.tts_service = tts_service
        app.state.memory_store = memory_store
        app.state.llm_provider = llm_provider
        memory_retriever = MemoryRAGRetriever(memory_store=memory_store, settings=resolved_settings)
        app.state.rag_retriever = rag_retriever
        app.state.memory_retriever = memory_retriever
        app.state.agent_pool = agent_pool
        app.state.chat_request_coordinator = request_coordinator
        app.state.chat_orchestrator = ChatOrchestrator(
            settings=resolved_settings,
            memory_store=memory_store,
            llm_provider=llm_provider,
            rag_retriever=rag_retriever,
            memory_retriever=memory_retriever,
            agent_factory=agent_factory,
            agent_pool=agent_pool,
            request_coordinator=request_coordinator,
        )
        app.state.expedition_orchestrator = ExpeditionOrchestrator(
            settings=resolved_settings,
            llm_provider=llm_provider,
            memory_store=memory_store,
            rag_retriever=rag_retriever,
            memory_retriever=memory_retriever,
            agent_factory=resolved_expedition_factory,
        )
        try:
            yield
        finally:
            expedition_orchestrator = getattr(app.state, "expedition_orchestrator", None)
            if expedition_orchestrator is not None:
                await expedition_orchestrator.close()
            if agent_pool is not None:
                await agent_pool.close()
            if tts_service is not None:
                await tts_service.close()

    app = FastAPI(title="Mirdo Server", version=resolved_settings.version, lifespan=lifespan)
    # TTS 是主后端里的独立 Provider；只有请求明确 use_tts=true 时，Chat 才会
    # 等待它生成 WAV，并在响应中返回可缓存的 audio_url。默认关闭时不增加延迟。
    app.include_router(tts_router)

    @app.get("/health")
    async def health() -> dict:
        memory_store = getattr(app.state, "memory_store", None)
        rag_retriever = getattr(app.state, "rag_retriever", None)
        memory_ready = bool(memory_store.health()) if memory_store is not None else False
        rag_ready = bool(rag_retriever.is_ready()) if rag_retriever is not None else False
        llm_ready = resolved_settings.llm_ready
        llm_provider = getattr(app.state, "llm_provider", None)
        if not llm_ready and llm_provider is not None:
            try:
                resolved_provider = llm_provider.resolve_provider(None)
                llm_ready = bool(resolved_provider.base_url and resolved_provider.model)
            except Exception:
                llm_ready = False
        return {
            "ok": True,
            "service": resolved_settings.service_name,
            "version": resolved_settings.version,
            "llm_ready": llm_ready,
            "rag_ready": rag_ready,
            "memory_ready": memory_ready,
            "runtime_dir": str(resolved_settings.runtime_dir),
            "knowledge_dir": str(resolved_settings.knowledge_dir),
            "host": resolved_settings.app_host,
            "port": resolved_settings.app_port,
            "tts_enabled": bool(getattr(getattr(app.state, "tts_settings", None), "enabled", False)),
        }

    @app.get("/model/probe")
    async def model_probe() -> dict:
        llm_provider: LLMProvider = app.state.llm_provider
        return await _probe_model(resolved_settings, llm_provider, None)

    @app.post("/model/probe")
    async def model_probe_with_provider(provider: ProviderConfig | None = None) -> dict:
        llm_provider: LLMProvider = app.state.llm_provider
        return await _probe_model(resolved_settings, llm_provider, provider)

    @app.post("/ingest")
    async def ingest(request: IngestRequest) -> dict:
        folder = Path(request.folder) if request.folder.strip() else resolved_settings.knowledge_dir
        indexer = RAGIndexer(resolved_settings.rag_db, folder, include_project_tree=resolved_settings.rag_include_project_tree)
        result = indexer.ingest(clear_first=request.clear_first)
        app.state.rag_retriever = RAGRetriever(settings=resolved_settings)
        app.state.chat_orchestrator.deps.rag_retriever = app.state.rag_retriever
        app.state.expedition_orchestrator.rag_retriever = app.state.rag_retriever
        return result

    @app.post("/chat")
    async def chat(request: ChatRequest) -> dict:
        app.state.chat_request_coordinator.register(request)
        orchestrator: ChatOrchestrator = app.state.chat_orchestrator
        response = await orchestrator.chat_async(request)
        if not response.superseded:
            response = await attach_tts_to_response(app.state.tts_service, request, response)
        return response.model_dump(mode="json")

    @app.post("/godot/action-result")
    async def godot_action_result(request: GodotActionResultRequest) -> dict:
        """接收 Godot 当前工具调用的结果，并返回 Agent 的下一步。

        Godot 只有在动作真正完成（或失败）后才调用此接口；Server 不轮询
        游戏状态，也不把动作结果伪装成玩家发言。Graph 会更新等待中的任务，
        把 ``event_context`` 注入 Mirdo 的 instructions，再正常走 tools、记忆
        和 action_line 校验，最后一次性返回下一步响应。
        """
        app.state.chat_request_coordinator.register(request)
        protocol = request.model_dump(mode="json", exclude={"context", "provider"})
        source_decision = dict(request.source_decision)
        # source_decision 是 Graph 任务校验的可信入口；缺失字段由协议字段补齐。
        for key in (
            "tool_call_id",
            "task_id",
            "chain_id",
            "step_id",
            "command",
            "target_ref",
            "event",
            "status",
            "ok",
            "action_result",
            "execution",
            "observation",
        ):
            if key not in source_decision and key in protocol:
                source_decision[key] = protocol[key]
        event_context = dict(request.context.get("event_context", {})) if isinstance(request.context.get("event_context"), dict) else {}
        for key in (
            "tool_call_id",
            "task_id",
            "chain_id",
            "step_id",
            "command",
            "target_ref",
            "event",
            "status",
            "ok",
            "action_result",
            "execution",
            "observation",
        ):
            if key in protocol:
                event_context[key] = protocol[key]
        context = {
            **request.context,
            "request_source": "godot_tool_result",
            "source_decision": source_decision,
            "event": request.event,
            "event_context": event_context,
            "godot_tool_result": protocol,
        }
        internal_prompt = "（Godot 工具结果：%s；请依据真实结果决定下一步。）" % request.event
        chat_request = ChatRequest(
            session_id=request.session_id,
            player_text=internal_prompt,
            day=request.day,
            time=request.time,
            time_min=request.time_min,
            npc_stats=request.npc_stats,
            given_item=request.given_item,
            context=context,
            use_tts=request.use_tts,
            tts_voice_profile=request.tts_voice_profile,
            tts_speaker_id=request.tts_speaker_id,
            tts_audio_delivery=request.tts_audio_delivery,
            tts_inline_audio=request.tts_inline_audio,
            tts_inline_max_bytes=request.tts_inline_max_bytes,
            generate_japanese=request.generate_japanese,
            provider=request.provider,
            client_request_id=request.client_request_id,
            client_sequence=request.client_sequence,
            supersedes_request_id=request.supersedes_request_id,
        )
        orchestrator: ChatOrchestrator = app.state.chat_orchestrator
        response = await orchestrator.chat_async(chat_request)
        if not response.superseded:
            response = await attach_tts_to_response(app.state.tts_service, chat_request, response)
        response.response_kind = "godot_tool_result"
        response.tool_call_id = request.tool_call_id
        response.tool_result_ack = {
            "ok": request.ok,
            "status": request.status,
            "event": request.event,
            "task_id": request.task_id,
            "step_id": request.step_id,
            "action_result": request.action_result,
            "execution": request.execution,
            "observation": request.observation,
        }
        return response.model_dump(mode="json")

    @app.post("/outing/resolve")
    async def outing_resolve(request: ExpeditionRequest) -> dict:
        orchestrator: ExpeditionOrchestrator = app.state.expedition_orchestrator
        response = await run_in_threadpool(orchestrator.resolve, request)
        return response.model_dump(mode="json")

    @app.get("/sessions")
    async def sessions(limit: int = 100) -> dict:
        memory_store: MemoryStore = app.state.memory_store
        return {"ok": True, "sessions": memory_store.list_sessions(limit)}

    @app.get("/memory/{session_id}")
    async def memory_facts(session_id: str, limit: int = 50) -> dict:
        memory_store: MemoryStore = app.state.memory_store
        safe_limit = max(1, min(int(limit), 200))
        return {
            "ok": True,
            "session_id": str(session_id or "default_session").strip() or "default_session",
            "memory_facts": [fact.to_dict() for fact in memory_store.get_memory_facts(session_id, safe_limit)],
        }

    @app.get("/memory/{session_id}/search")
    async def memory_search(session_id: str, q: str = Query(default=""), limit: int = 12) -> dict:
        memory_store: MemoryStore = app.state.memory_store
        memory_retriever = getattr(app.state, "memory_retriever", None)
        safe_limit = max(1, min(int(limit), 50))
        if memory_retriever is not None:
            facts = list(memory_retriever.retrieve(session_id, q, top_k=safe_limit))
        else:
            facts = [fact.to_dict() for fact in memory_store.search_memory_facts(session_id, q, safe_limit)]
        return {
            "ok": True,
            "session_id": str(session_id or "default_session").strip() or "default_session",
            "query": str(q or ""),
            "memory_facts": facts,
        }

    @app.delete("/memory/{session_id}/facts/{fact_id}")
    async def delete_memory_fact(session_id: str, fact_id: int) -> dict:
        memory_store: MemoryStore = app.state.memory_store
        result = memory_store.delete_memory_fact(session_id, fact_id)
        memory_retriever = getattr(app.state, "memory_retriever", None)
        if memory_retriever is not None and result.get("deleted"):
            memory_retriever.clear_session_index(session_id)
        return result

    @app.get("/rag/status")
    async def rag_status() -> dict:
        rag_retriever: RAGRetriever = app.state.rag_retriever
        return rag_retriever.status()

    @app.delete("/rag/clear")
    async def rag_clear() -> dict:
        rag_retriever: RAGRetriever = app.state.rag_retriever
        return rag_retriever.clear()

    @app.get("/session/{session_id}/history")
    async def session_history(session_id: str, limit: int = 40) -> dict:
        memory_store: MemoryStore = app.state.memory_store
        return memory_store.get_session_history(session_id, limit)

    @app.get("/session/{session_id}/snapshot")
    async def session_snapshot(session_id: str, recent_limit: int = 20) -> dict:
        memory_store: MemoryStore = app.state.memory_store
        return memory_store.get_session_snapshot(session_id, recent_limit)

    @app.delete("/session/{session_id}")
    async def delete_session(session_id: str) -> dict:
        """删除会话的完整数据：对话、摘要、记忆、故事和未完成任务。"""
        memory_store: MemoryStore = app.state.memory_store
        result = memory_store.clear_session(session_id)
        memory_retriever = getattr(app.state, "memory_retriever", None)
        result["memory_index_deleted"] = (
            memory_retriever.clear_session_index(session_id)
            if memory_retriever is not None else 0
        )
        result["deleted"] = any(
            int(result.get(key, 0)) > 0
            for key in (
                "turns_deleted", "facts_deleted", "story_events_deleted",
                "navigation_tasks_deleted",
            )
        )
        return result

    @app.post("/memory/clear")
    async def clear_memory(request: MemoryClearRequest) -> dict:
        memory_store: MemoryStore = app.state.memory_store
        memory_retriever = getattr(app.state, "memory_retriever", None)
        if request.clear_all:
            result = memory_store.clear_all()
            result["memory_index_deleted"] = memory_retriever.clear_all_index() if memory_retriever is not None else 0
            return result
        result = memory_store.clear_session(request.session_id)
        result["memory_index_deleted"] = memory_retriever.clear_session_index(request.session_id) if memory_retriever is not None else 0
        return result

    return app


app = create_app()


async def _probe_model(settings: Settings, provider: LLMProvider, request_provider: ProviderConfig | None) -> dict[str, Any]:
    """用最小 PydanticAI Agent 验证当前服务商配置。"""
    try:
        resolved = provider.resolve_provider(request_provider)
        agent = build_probe_agent(settings, resolved)
        try:
            async with agent:
                output = (await agent.run("ping")).output.strip()
        finally:
            await agent.model.client.close()
        return {
            "ok": bool(output), "base_url": resolved.base_url, "model": resolved.model,
            "proxy_enabled": bool(resolved.proxy_url), "content_preview": output[:120],
        }
    except ModelHTTPError as exc:
        status_code = getattr(exc, "status_code", 0)
        body = getattr(exc, "body", None)
        hint = ""
        if status_code == 404 and not str(request_provider.base_url if request_provider else settings.api_base_url).rstrip("/").endswith("/v1"):
            hint = "Base URL 可能缺少 /v1，例如 http://127.0.0.1:8317/v1"
        return {
            "ok": False,
            "error": "ModelHTTPError",
            "status_code": status_code,
            "message": str(exc),
            "hint": hint,
            "body_preview": str(body)[:240] if body is not None else "",
            "content_preview": "",
        }
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "message": str(exc), "content_preview": ""}

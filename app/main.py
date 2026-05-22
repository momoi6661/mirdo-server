from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from starlette.concurrency import run_in_threadpool

from .chat_orchestrator import ChatOrchestrator
from .config import Settings, get_settings
from .expedition_orchestrator import ExpeditionOrchestrator
from .llm_provider import LLMProvider, ResolvedProvider
from .memory.retriever import MemoryRAGRetriever
from .memory.store import MemoryStore
from .rag.indexer import RAGIndexer
from .rag.retriever import RAGRetriever
from .schemas import ChatRequest, ExpeditionRequest, IngestRequest, MemoryClearRequest, ProviderConfig


def create_app(
    settings: Settings | None = None,
    chat_model_factory: Callable[[ResolvedProvider], Any] | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings.ensure_runtime_dirs()
        memory_store = MemoryStore(resolved_settings.conversation_db)
        memory_store.initialize()
        llm_provider = LLMProvider(resolved_settings, chat_model_factory=chat_model_factory)
        rag_retriever = RAGRetriever(resolved_settings.chroma_dir, settings=resolved_settings)
        app.state.settings = resolved_settings
        app.state.memory_store = memory_store
        app.state.llm_provider = llm_provider
        memory_retriever = MemoryRAGRetriever(memory_store=memory_store, chroma_dir=resolved_settings.chroma_dir, settings=resolved_settings)
        app.state.rag_retriever = rag_retriever
        app.state.memory_retriever = memory_retriever
        app.state.chat_orchestrator = ChatOrchestrator(
            settings=resolved_settings,
            memory_store=memory_store,
            llm_provider=llm_provider,
            rag_retriever=rag_retriever,
            memory_retriever=memory_retriever,
        )
        app.state.expedition_orchestrator = ExpeditionOrchestrator(
            settings=resolved_settings,
            llm_provider=llm_provider,
            memory_store=memory_store,
            rag_retriever=rag_retriever,
            memory_retriever=memory_retriever,
        )
        yield

    app = FastAPI(title="Server", version=resolved_settings.version, lifespan=lifespan)

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
        }

    @app.get("/model/probe")
    async def model_probe() -> dict:
        llm_provider: LLMProvider = app.state.llm_provider
        return llm_provider.probe_model()

    @app.post("/model/probe")
    async def model_probe_with_provider(provider: ProviderConfig | None = None) -> dict:
        llm_provider: LLMProvider = app.state.llm_provider
        return llm_provider.probe_model(provider)

    @app.post("/ingest")
    async def ingest(request: IngestRequest) -> dict:
        folder = Path(request.folder) if request.folder.strip() else resolved_settings.knowledge_dir
        indexer = RAGIndexer(knowledge_dir=folder, chroma_dir=resolved_settings.chroma_dir, settings=resolved_settings)
        result = indexer.ingest(clear_first=request.clear_first)
        app.state.rag_retriever = RAGRetriever(resolved_settings.chroma_dir, settings=resolved_settings)
        app.state.chat_orchestrator.rag_retriever = app.state.rag_retriever
        app.state.expedition_orchestrator.rag_retriever = app.state.rag_retriever
        return result

    @app.post("/chat")
    async def chat(request: ChatRequest) -> dict:
        orchestrator: ChatOrchestrator = app.state.chat_orchestrator
        response = await run_in_threadpool(orchestrator.chat, request)
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
            # Re-sync lazily on the next retrieval; remove stale vector now.
            memory_retriever.clear_session_vectors(session_id)
        return result

    @app.get("/rag/status")
    async def rag_status() -> dict:
        rag_retriever: RAGRetriever = app.state.rag_retriever
        return rag_retriever.status()

    @app.delete("/rag/clear")
    async def rag_clear() -> dict:
        rag_retriever: RAGRetriever = app.state.rag_retriever
        result = rag_retriever.clear()
        app.state.rag_retriever = RAGRetriever(resolved_settings.chroma_dir, settings=resolved_settings)
        app.state.chat_orchestrator.rag_retriever = app.state.rag_retriever
        app.state.expedition_orchestrator.rag_retriever = app.state.rag_retriever
        return result

    @app.get("/session/{session_id}/history")
    async def session_history(session_id: str, limit: int = 40) -> dict:
        memory_store: MemoryStore = app.state.memory_store
        return memory_store.get_session_history(session_id, limit)

    @app.get("/session/{session_id}/snapshot")
    async def session_snapshot(session_id: str, recent_limit: int = 20) -> dict:
        memory_store: MemoryStore = app.state.memory_store
        return memory_store.get_session_snapshot(session_id, recent_limit)

    @app.post("/memory/clear")
    async def clear_memory(request: MemoryClearRequest) -> dict:
        memory_store: MemoryStore = app.state.memory_store
        memory_retriever = getattr(app.state, "memory_retriever", None)
        if request.clear_all:
            result = memory_store.clear_all()
            result["memory_vectors_deleted"] = memory_retriever.clear_all_vectors() if memory_retriever is not None else 0
            return result
        result = memory_store.clear_session(request.session_id)
        result["memory_vectors_deleted"] = memory_retriever.clear_session_vectors(request.session_id) if memory_retriever is not None else 0
        return result

    return app


app = create_app()

from __future__ import annotations

from typing import Any
from uuid import uuid4
import time

from .config import Settings
from .character_ai import CharacterBehaviorPlanner
from .llm_provider import LLMProvider
from .memory.extractor import MemoryExtractor
from .memory.store import MemoryStore
from .prompt_builder import PromptBuilder
from .response_parser import ResponseParser
from .schemas import ChatRequest, ChatResponse


class ChatOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        memory_store: MemoryStore,
        llm_provider: LLMProvider,
        prompt_builder: PromptBuilder | None = None,
        response_parser: ResponseParser | None = None,
        rag_retriever: Any | None = None,
        memory_retriever: Any | None = None,
        memory_extractor: MemoryExtractor | None = None,
        behavior_planner: CharacterBehaviorPlanner | None = None,
    ) -> None:
        self.settings = settings
        self.memory_store = memory_store
        self.llm_provider = llm_provider
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.response_parser = response_parser or ResponseParser()
        self.rag_retriever = rag_retriever
        self.memory_retriever = memory_retriever
        self.memory_extractor = memory_extractor or MemoryExtractor()
        self.behavior_planner = behavior_planner or CharacterBehaviorPlanner()

    def chat(self, request: ChatRequest) -> ChatResponse:
        started = time.perf_counter()
        request, fork_info = self._resolve_timeline_for_write(request)
        self._log_timing("request_start", started, request.session_id)
        user_turn = self.memory_store.add_turn(
            request.session_id,
            "user",
            request.player_text,
            request.model_dump(mode="json"),
        )
        self._log_timing("user_turn_saved", started, request.session_id)
        recent_turns = self.memory_store.get_recent_turns(request.session_id, limit=request.max_context_turns)
        memory_facts = self._retrieve_memory(request)
        self._log_timing("memory_loaded", started, request.session_id)
        knowledge_hits = self._retrieve_knowledge(request)
        self._log_timing("rag_done hits=%d" % len(knowledge_hits), started, request.session_id)
        messages = self.prompt_builder.build(
            request=request,
            recent_turns=recent_turns,
            memory_facts=memory_facts,
            knowledge_hits=knowledge_hits,
        )
        self._log_timing("prompt_built messages=%d" % len(messages), started, request.session_id)

        memory_updates: list[dict[str, Any]] = []
        memory_updates.extend(self.memory_extractor.extract(request.player_text))

        try:
            chat_model = self.llm_provider.build_chat_model(request.provider, json_mode=True)
            self._log_timing("invoke_start", started, request.session_id)
            model_message = chat_model.invoke(messages)
            self._log_timing("invoke_done", started, request.session_id)
            raw_text = str(getattr(model_message, "content", "") or "")
            parsed = self.response_parser.parse(raw_text, session_id=request.session_id)
            self._log_timing("parse_done chars=%d" % len(raw_text), started, request.session_id)
            memory_updates.extend(self.memory_extractor.extract_model_updates(parsed.memory_updates))
            parsed.used_knowledge = knowledge_hits
            parsed.used_memory = memory_facts
        except Exception as exc:
            self._log_timing("failure:%s:%s" % (exc.__class__.__name__, self._compact_error(exc)), started, request.session_id)
            parsed = self.behavior_planner.local_fallback_response(request) or self._local_fallback_response(request)
            parsed.error = "model_call_failed"
            parsed.used_knowledge = knowledge_hits
            parsed.used_memory = memory_facts

        parsed = self.behavior_planner.finalize_response(request, parsed)
        stored_memory_updates = self._store_memory_updates(request.session_id, memory_updates, user_turn.id)
        self._log_timing("memory_stored updates=%d" % len(stored_memory_updates), started, request.session_id)
        parsed.memory_updates = stored_memory_updates

        assistant_turn = self.memory_store.add_turn(
            request.session_id,
            "assistant",
            parsed.dialogue,
            parsed.model_dump(mode="json"),
        )
        parsed.session_id = request.session_id
        parsed.turn_id = assistant_turn.id
        if fork_info:
            parsed.forked_from = str(fork_info.get("forked_from", ""))
            parsed.forked_at_turn_id = int(fork_info.get("forked_at_turn_id", 0))
        self._log_timing("response_done", started, request.session_id)
        return parsed


    def _resolve_timeline_for_write(self, request: ChatRequest) -> tuple[ChatRequest, dict[str, Any]]:
        checkpoint = self._request_checkpoint_turn_id(request)
        if checkpoint <= 0:
            return request, {}
        latest_turn_id = self.memory_store.get_latest_turn_id(request.session_id)
        if latest_turn_id <= 0 or checkpoint >= latest_turn_id:
            return request, {}
        forked_session = self._build_branch_session_id(request.session_id)
        self.memory_store.fork_session(request.session_id, checkpoint, forked_session)
        if self.memory_retriever is not None:
            try:
                self.memory_retriever.clear_session_vectors(forked_session)
            except Exception:
                pass
        data = request.model_dump(mode="python")
        context = dict(data.get("context") or {})
        context["forked_from"] = request.session_id
        context["forked_at_turn_id"] = checkpoint
        context["ai_checkpoint_turn_id"] = 0
        data["context"] = context
        data["session_id"] = forked_session
        forked_request = ChatRequest(**data)
        return forked_request, {"forked_from": request.session_id, "forked_at_turn_id": checkpoint}

    def _request_checkpoint_turn_id(self, request: ChatRequest) -> int:
        context = request.context if isinstance(request.context, dict) else {}
        raw = context.get("ai_checkpoint_turn_id", context.get("checkpoint_turn_id", 0))
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def _build_branch_session_id(self, source_session_id: str) -> str:
        base = str(source_session_id or "default_session").strip() or "default_session"
        for _attempt in range(8):
            candidate = f"{base}:branch_{uuid4().hex[:10]}"
            if self.memory_store.get_latest_turn_id(candidate) == 0:
                return candidate
        return f"{base}:branch_{uuid4().hex}"

    def _log_timing(self, stage: str, started: float, session_id: str) -> None:
        print("[ChatAI] timing %-28s %.2fs session=%s" % (stage, time.perf_counter() - started, session_id), flush=True)

    def _compact_error(self, exc: Exception) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        text = text.replace("\n", " ").replace("\r", " ")
        if len(text) > 120:
            text = text[:117] + "..."
        return text

    def _retrieve_memory(self, request: ChatRequest) -> list[dict[str, Any]]:
        if self.memory_retriever is not None:
            try:
                return list(self.memory_retriever.retrieve(request.session_id, request.player_text, top_k=12))
            except Exception:
                pass
        return [fact.to_dict() for fact in self.memory_store.search_memory_facts(request.session_id, request.player_text, limit=12)]

    def _retrieve_knowledge(self, request: ChatRequest) -> list[dict[str, Any]]:
        if self.rag_retriever is None:
            return []
        try:
            return list(self.rag_retriever.retrieve(request.player_text, top_k=self.settings.top_k))
        except Exception:
            return []

    def _store_memory_updates(
        self,
        session_id: str,
        memory_updates: list[dict[str, Any]],
        source_turn_id: int,
    ) -> list[dict[str, Any]]:
        stored: list[dict[str, Any]] = []
        for update in memory_updates:
            try:
                fact = self.memory_store.upsert_memory_fact(
                    session_id=session_id,
                    subject=str(update.get("subject", "player")),
                    predicate=str(update.get("predicate", "related_to")),
                    value=str(update.get("value", "")),
                    confidence=float(update.get("confidence", 0.75)),
                    source_turn_id=source_turn_id,
                )
            except (TypeError, ValueError):
                continue
            stored.append(fact.to_dict())
        return stored

    def _local_fallback_response(self, request: ChatRequest) -> ChatResponse:
        stats = request.npc_stats
        npc_name = self._npc_name(request)
        if stats.thirst <= 25:
            dialogue = "老师，通讯信号断了。Mirdo 有点渴，想先确认饮水。"
            emotion = "担心"
            expression = "sorrow"
        elif stats.hunger <= 25:
            dialogue = "老师，模型那边暂时没有回应。Mirdo 有点饿，但会继续陪着你。"
            emotion = "关心"
            expression = "sorrow"
        elif stats.mood <= 25:
            dialogue = "信号断断续续的……没关系，老师，我在这里，我们慢慢来。"
            emotion = "安抚"
            expression = "sorrow"
        else:
            dialogue = f"信号不太稳定，老师。{npc_name}先按当前情况陪你行动，等连接恢复再细说。"
            emotion = "冷静"
            expression = "neutral"
        return ChatResponse(
            ok=True,
            dialogue=dialogue,
            emotion=emotion,
            expression=expression,
            action="Idle",
            command="",
            command_payload={},
            visemes="",
            viseme_sequence="",
            stat_change={},
            memory_tags=["local_fallback"],
            session_id=request.session_id,
            turn_id=0,
            used_knowledge=[],
            used_memory=[],
            memory_updates=[],
            fallback=True,
            error="model_call_failed",
        )

    def _npc_name(self, request: ChatRequest) -> str:
        context = request.context if isinstance(request.context, dict) else {}
        npc = context.get("npc", {})
        if isinstance(npc, dict):
            name = str(npc.get("name", "")).strip()
            if name:
                return name
        return "我"

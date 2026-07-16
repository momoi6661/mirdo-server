"""外出结算 Agent。

外出与聊天共用 PydanticAI 的模型适配、tool loop 与结构化校验；没有第二套 JSON 协议。
"""
from __future__ import annotations

import json
import re
import asyncio
import threading
from typing import Any
from uuid import uuid4

from pydantic_ai import UsageLimits

from .config import Settings
from .expedition_agent import build_expedition_agent
from .llm_provider import LLMProvider
from .memory.store import MemoryStore
from .mirdo_agent import AgentContext, AgentFactory
from .model_errors import classify_model_error
from .rag.retriever import RAGRetriever
from .schemas import ExpeditionRequest, ExpeditionResponse, ExpeditionStoryMarker


class ExpeditionOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        llm_provider: LLMProvider,
        memory_store: MemoryStore | None = None,
        rag_retriever: RAGRetriever | None = None,
        memory_retriever: Any | None = None,
        agent_factory: AgentFactory = build_expedition_agent,
    ) -> None:
        self.settings = settings
        self.llm_provider = llm_provider
        self.memory_store = memory_store
        self.rag_retriever = rag_retriever
        self.memory_retriever = memory_retriever
        self.agent_factory = agent_factory
        # 外出请求在线程池中同步执行，因此使用线程安全的 Agent 缓存复用同一条连接。
        self._agent_cache: dict[tuple[str, str, str, str], Any] = {}
        self._agent_cache_lock = threading.Lock()

    def resolve(self, request: ExpeditionRequest) -> ExpeditionResponse:
        request, fork_info = self._resolve_timeline_for_write(request)
        user_turn_id = self._record_turn(
            request,
            "user",
            "主角外出探索：%s" % (request.location.name or request.location.id),
            request.model_dump(mode="json"),
        )
        context = self._load_expedition_context(request)
        try:
            resolved_provider = self.llm_provider.resolve_provider(request.provider)
            agent = self._get_agent(resolved_provider)
            prompt = self._build_prompt(request, context)
            # PydanticAI 已按 ExpeditionResponse 校验输出，绝不读取/修复裸 JSON 字符串。
            response = agent.run_sync(
                prompt,
                deps=AgentContext(
                    request,
                    self.rag_retriever,
                    self.memory_retriever,
                    memory_store=self.memory_store,
                ),
                usage_limits=UsageLimits(request_limit=4),
            ).output
            response = self._sanitize_loot(response, request)
            response = self._sanitize_focus(response, context)
        except Exception as exc:
            response = self._model_failure_response(request, exc)
        turn_id = self._record_turn(request, "assistant", response.summary or response.story, response.model_dump(mode="json"))
        self._persist_story(response, request, turn_id or user_turn_id, context)
        self._finalize(response, request, turn_id, fork_info)
        return response

    def _get_agent(self, resolved_provider: Any) -> Any:
        """按服务商配置复用 GM Agent，避免每次外出都新建 AsyncOpenAI 客户端。"""
        key = (
            str(resolved_provider.base_url),
            str(resolved_provider.api_key),
            str(resolved_provider.model),
            str(resolved_provider.proxy_url),
        )
        with self._agent_cache_lock:
            agent = self._agent_cache.get(key)
            if agent is None:
                agent = self.agent_factory(self.settings, resolved_provider, ExpeditionResponse)
                self._agent_cache[key] = agent
            return agent

    async def close(self) -> None:
        """服务关闭时释放 GM Agent 持有的 PydanticAI/OpenAI 客户端。"""
        with self._agent_cache_lock:
            agents = list(self._agent_cache.values())
            self._agent_cache.clear()
        for agent in agents:
            client = getattr(getattr(agent, "model", None), "client", None)
            close = getattr(client, "close", None)
            if close is None:
                continue
            result = close()
            if asyncio.iscoroutine(result):
                await result

    def _load_expedition_context(self, request: ExpeditionRequest) -> dict[str, Any]:
        """为 GM 汇总可追溯上下文：对话偏好、地点状态、旧故事和知识库资料。"""
        if self.memory_store is None:
            recent_turns: list[Any] = []
            facts: list[Any] = []
            story_events: list[dict[str, Any]] = []
            session_summary = ""
        else:
            recent_turns = self.memory_store.get_recent_turns(request.session_id, limit=24)
            facts = self.memory_store.get_memory_facts(request.session_id, limit=50)
            story_events = self.memory_store.get_story_events(request.session_id, limit=30)
            session_summary, _summary_turn_id = self.memory_store.get_session_summary(request.session_id)
        focus = self._infer_search_focus(request, recent_turns, facts, session_summary)
        location_id = str(request.location.id or request.location.name).strip()
        active_events = []
        for event in story_events:
            metadata = event.get("metadata", {}) if isinstance(event, dict) else {}
            event_location = str(metadata.get("location_id", "")).strip()
            # 日常对话事件仍属于总故事，但不能被误当成每个外出地点的未完成线索。
            same_location = bool(location_id) and (
                event_location == location_id or (not event_location and event.get("kind") == "expedition")
            )
            status = str(metadata.get("status", "active"))
            if same_location and status not in {"resolved", "closed", "superseded"}:
                active_events.append(event)
        query = " ".join([request.location.name, request.location.description, *focus]).strip()
        knowledge = []
        if self.rag_retriever is not None and query:
            try:
                knowledge = list(self.rag_retriever.retrieve(query, top_k=6))
            except Exception:
                knowledge = []
        return {
            "search_focus": focus,
            "recent_dialogue": [
                {"role": turn.role, "content": turn.content, "turn_id": turn.id}
                for turn in recent_turns[-12:]
                if turn.content
            ],
            "session_summary": session_summary,
            "confirmed_memory": [fact.to_dict() for fact in facts if fact.predicate in {"wants", "likes", "dislikes", "note"}],
            "previous_story_events": story_events[:12],
            "active_location_threads": active_events[:8],
            "knowledge": knowledge,
            "request_context": request.context if isinstance(request.context, dict) else {},
        }

    def _infer_search_focus(
        self,
        request: ExpeditionRequest,
        turns: list[Any],
        facts: list[Any],
        session_summary: str = "",
    ) -> list[str]:
        """从明确表达中提炼搜索重点；不把 GM 的猜测冒充成玩家事实。"""
        candidates: list[str] = []
        for fact in facts:
            if str(getattr(fact, "predicate", "")) == "wants":
                candidates.append(str(getattr(fact, "value", "")))
        explicit = re.compile(r"(?:想找|想要|想拿|需要|缺少|寻找|最好(?:再)?拿|再拿)\s*([^，。,.！!；;\n]{1,24})")
        for turn in turns:
            if getattr(turn, "role", "") != "user":
                continue
            for match in explicit.finditer(str(getattr(turn, "content", ""))):
                value = re.sub(r"^(?:去|找|拿|一个|一些|一份|点)\s*", "", match.group(1)).strip()
                if value:
                    candidates.append(value)
        for match in explicit.finditer(str(session_summary or "")):
            value = re.sub(r"^(?:去|找|拿|一个|一些|一份|点)\s*", "", match.group(1)).strip()
            if value:
                candidates.append(value)
        context = request.context if isinstance(request.context, dict) else {}
        supplied = context.get("search_focus", [])
        if isinstance(supplied, list):
            candidates.extend(str(item).strip() for item in supplied if str(item).strip())
        result: list[str] = []
        for value in candidates:
            clean = str(value).strip()[:40]
            if clean and clean not in result:
                result.append(clean)
        return result[:8]

    def _build_prompt(self, request: ExpeditionRequest, context: dict[str, Any]) -> str:
        """把结构化上下文交给 GM；具体输出格式由 PydanticAI 的 output_type 负责。"""
        payload = request.model_dump(mode="json")
        return "\n".join(
            [
                "你是 GM。外出主体是玩家/主角，不是 Mirdo；请生成一段可继续的探索故事。",
                "先读取 active_location_threads 和 previous_story_events，再使用 search_focus 作为候选重点。",
                "同一地点已有 active 线索时必须优先延续；只有在证据允许时才标记 resolved。",
                "面向玩家的文字使用中文，按动机、过程、发现、风险和返程后果叙述。",
                "只可选择 available_loot 中存在的物资路径，不得编造地点或物品。",
                "本次行动输入（事实）：",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                "本存档的外出上下文（记忆和故事）：",
                json.dumps(context, ensure_ascii=False, separators=(",", ":")),
            ]
        )

    def _persist_story(
        self,
        response: ExpeditionResponse,
        request: ExpeditionRequest,
        turn_id: int,
        context: dict[str, Any],
    ) -> None:
        """将成功外出的摘要和标记写入故事记忆，供下次 GM 接续。"""
        if self.memory_store is None or not response.ok or response.fallback:
            return
        markers = list(response.story_markers)
        if not markers:
            markers = [
                ExpeditionStoryMarker(
                    continuity_key=f"location:{request.location.id or request.location.name}",
                    kind="expedition",
                    summary=response.summary or response.story,
                    location_id=request.location.id or request.location.name,
                    status="active" if response.discovered_clues else "resolved",
                    tags=["expedition"],
                    next_hooks=list(response.discovered_clues[:3]),
                    importance=0.65,
                )
            ]
        for marker in markers[:8]:
            summary = marker.summary.strip() or response.summary.strip() or response.story.strip()
            if not summary:
                continue
            continuity_key = marker.continuity_key or f"location:{request.location.id or request.location.name}"
            # 新结果覆盖同一线索的旧 active 版本，但历史记录仍保留供回放。
            self.memory_store.supersede_story_events(
                request.session_id,
                continuity_key,
                status="resolved" if marker.status in {"resolved", "closed"} else "superseded",
            )
            self.memory_store.add_story_event(
                request.session_id,
                "expedition",
                summary,
                importance=marker.importance,
                source_turn_id=turn_id,
                metadata={
                    "continuity_key": continuity_key,
                    "kind": marker.kind,
                    "location_id": marker.location_id or request.location.id or request.location.name,
                    "status": marker.status,
                    "tags": marker.tags,
                    "next_hooks": marker.next_hooks,
                    "search_focus": response.search_focus or context.get("search_focus", []),
                    "title": response.title,
                    # 事件摘要用于检索，story_excerpt 让下一次 GM 在需要时恢复具体场景细节。
                    "story_excerpt": response.story[:2200],
                    "discovered_clues": response.discovered_clues,
                },
            )

    def _sanitize_loot(self, response: ExpeditionResponse, request: ExpeditionRequest) -> ExpeditionResponse:
        valid = {path for paths in request.available_loot.values() for path in paths if str(path).strip()}
        response.loot = [entry for entry in response.loot if entry.item_path in valid][:12]
        response.fallback = False
        return response

    def _sanitize_focus(self, response: ExpeditionResponse, context: dict[str, Any]) -> ExpeditionResponse:
        """只保留对话或请求中出现过的搜索重点，防止 GM 凭空改变玩家目标。"""
        candidates = [str(item).strip() for item in context.get("search_focus", []) if str(item).strip()]
        if not candidates:
            response.search_focus = []
            return response
        selected: list[str] = []
        for raw in response.search_focus:
            value = str(raw).strip()
            if not value:
                continue
            if any(value == item or value in item or item in value for item in candidates):
                if value not in selected:
                    selected.append(value[:40])
        response.search_focus = selected or candidates[:8]
        return response

    def _model_failure_response(self, request: ExpeditionRequest, exc: Exception) -> ExpeditionResponse:
        return ExpeditionResponse(
            ok=False,
            title="外出 AI 结算失败",
            summary="后端已收到外出请求，但模型暂时没有完成结算。",
            experience=["行动没有写入物资或地图进展，请检查模型配置后重试。"],
            risk_result="行动保持在出发前状态。",
            fallback=True,
            error=classify_model_error(exc).code,
        )

    def _resolve_timeline_for_write(self, request: ExpeditionRequest) -> tuple[ExpeditionRequest, dict[str, Any]]:
        checkpoint = self._checkpoint(request)
        if checkpoint <= 0 or self.memory_store is None or checkpoint >= self.memory_store.get_latest_turn_id(request.session_id):
            return request, {}
        session_id = self._branch_session_id(request.session_id)
        self.memory_store.fork_session(request.session_id, checkpoint, session_id)
        copied = request.model_copy(update={"session_id": session_id})
        return copied, {"forked_from": request.session_id, "forked_at_turn_id": checkpoint}

    def _checkpoint(self, request: ExpeditionRequest) -> int:
        try:
            return max(0, int(getattr(request, "ai_checkpoint_turn_id", 0)))
        except (TypeError, ValueError):
            return 0

    def _branch_session_id(self, session_id: str) -> str:
        return "%s:branch_%s" % (session_id or "default_session", uuid4().hex[:10])

    def _record_turn(self, request: ExpeditionRequest, role: str, content: str, payload: dict[str, Any]) -> int:
        if self.memory_store is None:
            return 0
        return int(self.memory_store.add_turn(request.session_id, role, content, payload).id)

    def _finalize(self, response: ExpeditionResponse, request: ExpeditionRequest, turn_id: int, fork_info: dict[str, Any]) -> None:
        response.session_id, response.turn_id = request.session_id, turn_id
        response.forked_from = str(fork_info.get("forked_from", ""))
        response.forked_at_turn_id = int(fork_info.get("forked_at_turn_id", 0))

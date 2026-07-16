"""Chat 的完整 Pydantic Graph 工作流。

这里刻意区分两层 loop：``Agent.run`` 负责模型与 tools 的内层循环；本图负责
Godot 事件触发后的外层循环（读取状态 → 规划 → 交给 Godot 执行 → 保存结果）。
动作的真实结果会作为下一次 Godot 回调重新进入本图，因此不在 HTTP 请求中手写
无限 ``while`` 循环。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from uuid import uuid4

from pydantic_graph import GraphBuilder, ReducerContext, StepContext
from pydantic_ai import ModelMessagesTypeAdapter, UsageLimits
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from .dialogue_text import memory_extraction_text
from .mirdo_agent import AgentContext, AgentPool, build_summary_agent
from .model_errors import classify_model_error
from .schemas import ChatResponse


_CHAT_LOGGER = logging.getLogger("mirdo.chat")
_STATIC_AGENT_DOCUMENTS = {"mirdo_personality_bible.md", "mirdo_behavior_planning.md"}


@dataclass
class ChatGraphState:
    """一次聊天回合的共享数据；只由 graph steps 读写。"""

    request: Any
    started_at: float = field(default_factory=perf_counter)
    fork_info: dict[str, Any] = field(default_factory=dict)
    user_turn_id: int = 0
    recent_turns: list[Any] = field(default_factory=list)
    memory_updates: list[dict[str, Any]] = field(default_factory=list)
    story_events: list[dict[str, Any]] = field(default_factory=list)
    recalled_story_events: list[dict[str, Any]] = field(default_factory=list)
    memory: list[dict[str, Any]] = field(default_factory=list)
    knowledge: list[dict[str, Any]] = field(default_factory=list)
    resolved_provider: Any = None
    agent_messages_json: str = ""
    # Godot 工具结果不新增一条“玩家消息”，但仍然要进入同一个 Agent loop。
    is_tool_result: bool = False


@dataclass
class ChatGraphDeps:
    """每次 HTTP 请求传入 graph 的外部服务。"""

    settings: Any
    memory_store: Any
    llm_provider: Any
    agent_factory: Any
    agent_pool: AgentPool | None
    prompt_builder: Any
    rag_retriever: Any
    memory_retriever: Any
    memory_extractor: Any
    validator: Any
    request_coordinator: Any | None = None


@dataclass
class ChatReady:
    """玩家输入已保存，可以开始本回合行为规划。"""


@dataclass
class ContextLoadRequested:
    """通知 Graph 并行加载记忆和知识。"""


@dataclass
class MemoryContext:
    """记忆检索支路的输出。"""

    facts: list[dict[str, Any]]


@dataclass
class KnowledgeContext:
    """知识检索支路的输出。"""

    hits: list[dict[str, Any]]


@dataclass
class StoryContext:
    """共同经历检索支路的输出。"""

    events: list[dict[str, Any]]


@dataclass
class BehaviorPlanned:
    """PydanticAI 已返回并校验过 ChatResponse。"""

    response: ChatResponse
    new_messages_json: str = ""


@dataclass
class BehaviorPlanningFailed:
    """PydanticAI 的请求、tool 或输出校验失败。"""

    error: Exception


@dataclass
class TurnPersisted:
    """本回合已经安全写入数据库，之后可选择性刷新长会话摘要。"""

    response: ChatResponse


_builder = GraphBuilder(name="mirdo-chat", state_type=ChatGraphState, deps_type=ChatGraphDeps, output_type=ChatResponse)


@_builder.step(node_id="record_player_turn", label="保存玩家输入并准备上下文")
async def record_player_turn(ctx: StepContext[ChatGraphState, ChatGraphDeps, None]) -> ChatReady:
    """处理分支存档，并区分玩家输入与 Godot tool result。

    工具结果是已经发生的世界事实，不能伪装成玩家说过的话，否则长期记忆会
    出现“玩家说：导航成功”的重复内容；它只更新任务状态并复用现有历史。
    """
    state, deps = ctx.state, ctx.deps
    state.request, state.fork_info = _resolve_timeline(state.request, deps.memory_store, deps.memory_retriever)
    state.request = _attach_verified_task(state.request, deps.memory_store)
    state.is_tool_result = _is_godot_tool_result_request(state.request)
    _trace(
        deps,
        "input",
        session_id=state.request.session_id,
        request=state.request.model_dump(mode="json", exclude={"provider"}),
    )
    if state.is_tool_result:
        # 工具结果请求没有新的 user turn；保留上一条 turn 作为 agent history 的末尾。
        state.user_turn_id = int(deps.memory_store.get_latest_turn_id(state.request.session_id))
        state.memory_updates = []
    else:
        user_turn = deps.memory_store.add_turn(
            state.request.session_id,
            "user",
            state.request.player_text,
            state.request.model_dump(mode="json"),
        )
        state.user_turn_id = int(user_turn.id)
        state.memory_updates = list(deps.memory_extractor.extract(memory_extraction_text(state.request.player_text)))
    _summary, summary_turn_id = deps.memory_store.get_session_summary(state.request.session_id)
    history_limit = max(12, int(deps.settings.context_window_turns) * 2)
    state.recent_turns = deps.memory_store.get_turns_after(state.request.session_id, summary_turn_id, limit=history_limit)
    return ChatReady()


@_builder.step(node_id="load_context", label="加载记忆与知识上下文")
async def load_context(ctx: StepContext[ChatGraphState, ChatGraphDeps, ChatReady]) -> ContextLoadRequested:
    """发起上下文加载。

    后续两个 retrieval step 会由 broadcast 并行执行，再由 join/reducer 汇合。
    """
    return ContextLoadRequested()


@_builder.step(node_id="retrieve_memory", label="并行检索长期记忆")
async def retrieve_memory(ctx: StepContext[ChatGraphState, ChatGraphDeps, ContextLoadRequested]) -> MemoryContext:
    """在线程中查询 SQLite 记忆，避免阻塞知识检索支路。"""
    retriever = ctx.deps.memory_retriever
    if retriever is None:
        return MemoryContext([])
    facts = await asyncio.to_thread(retriever.retrieve, ctx.state.request.session_id, ctx.state.request.player_text, 12)
    return MemoryContext(list(facts))


@_builder.step(node_id="retrieve_knowledge", label="并行检索知识库")
async def retrieve_knowledge(ctx: StepContext[ChatGraphState, ChatGraphDeps, ContextLoadRequested]) -> KnowledgeContext:
    """在线程中查询 FTS5 知识库，和记忆检索同时进行。"""
    retriever = ctx.deps.rag_retriever
    if retriever is None:
        return KnowledgeContext([])
    hits = await asyncio.to_thread(retriever.retrieve, ctx.state.request.player_text, 4)
    # 人格与行为规划已作为 Agent 的稳定 instructions 加载，避免 RAG 再重复一次。
    return KnowledgeContext(
        [
            hit
            for hit in hits
            if str(hit.get("source", "")).replace("\\", "/").rsplit("/", 1)[-1] not in _STATIC_AGENT_DOCUMENTS
        ]
    )


@_builder.step(node_id="retrieve_story_events", label="并行读取共同经历")
async def retrieve_story_events(ctx: StepContext[ChatGraphState, ChatGraphDeps, ContextLoadRequested]) -> StoryContext:
    """读取最近共同经历，让 Mirdo 能把生活点滴接入当前回合。"""
    events = await asyncio.to_thread(ctx.deps.memory_store.get_story_events, ctx.state.request.session_id, 6)
    return StoryContext(list(events))


def merge_context(
    _ctx: ReducerContext[ChatGraphState, ChatGraphDeps],
    current: list[MemoryContext | KnowledgeContext | StoryContext],
    incoming: MemoryContext | KnowledgeContext | StoryContext,
) -> list[MemoryContext | KnowledgeContext | StoryContext]:
    """join 的 reducer：收集并行检索结果，等待三条支路都结束。"""
    return [*current, incoming]


_context_join = _builder.join(merge_context, initial_factory=list, node_id="join_context")


@_builder.step(node_id="plan_behavior", label="由 Mirdo Agent 规划本回合行为")
async def plan_behavior(
    ctx: StepContext[ChatGraphState, ChatGraphDeps, list[MemoryContext | KnowledgeContext | StoryContext]],
) -> BehaviorPlanned | BehaviorPlanningFailed:
    """合并并行上下文后调用 PydanticAI Agent，完成本回合的内层 tool loop。"""
    state, deps = ctx.state, ctx.deps
    for result in ctx.inputs:
        if isinstance(result, MemoryContext):
            state.memory = result.facts
        elif isinstance(result, KnowledgeContext):
            state.knowledge = result.hits
        elif isinstance(result, StoryContext):
            state.recalled_story_events = result.events
    agent_acquire_ms = 0
    agent_run_started = 0.0
    try:
        state.resolved_provider = deps.llm_provider.resolve_provider(state.request.provider)
        _trace(
            deps,
            "context",
            session_id=state.request.session_id,
            model=state.resolved_provider.model,
            message_history_turns=len(state.recent_turns[:-1]),
            memory=state.memory,
            knowledge_sources=[str(hit.get("source", "")) for hit in state.knowledge],
            story_events=state.recalled_story_events,
        )
        agent_acquire_started = perf_counter()
        if deps.agent_pool is None:
            agent = deps.agent_factory(deps.settings, state.resolved_provider, ChatResponse)
        else:
            agent = await deps.agent_pool.get(
                "chat",
                state.resolved_provider,
                lambda: deps.agent_factory(deps.settings, state.resolved_provider, ChatResponse),
            )
        agent_acquire_ms = int((perf_counter() - agent_acquire_started) * 1000)
        summary, _summary_turn_id = deps.memory_store.get_session_summary(state.request.session_id)
        runtime_instructions = deps.prompt_builder.build(
            request=state.request,
            memory_facts=state.memory,
            knowledge_hits=state.knowledge,
            story_events=state.recalled_story_events,
            session_summary=summary,
        )
        agent_run_started = perf_counter()
        # 普通聊天刚刚写入了最后一条 user turn，所以排除它；tool result 没有
        # 新 turn，必须保留完整历史，避免丢掉最近一轮 Mirdo 回复。
        history_turns = state.recent_turns if state.is_tool_result else state.recent_turns[:-1]
        result = await agent.run(
            state.request.player_text,
            message_history=_agent_message_history(history_turns),
            instructions=runtime_instructions,
            usage_limits=UsageLimits(request_limit=4),
            deps=AgentContext(
                state.request,
                deps.rag_retriever,
                deps.memory_retriever,
                memory_store=deps.memory_store,
                pending_facts=state.memory_updates,
                pending_story_events=state.story_events,
            ),
        )
        _trace(
            deps,
            "model_timing",
            session_id=state.request.session_id,
            agent_acquire_ms=agent_acquire_ms,
            agent_run_ms=int((perf_counter() - agent_run_started) * 1000),
        )
    except Exception as exc:
        _trace(
            deps,
            "model_failure",
            session_id=state.request.session_id,
            error_type=exc.__class__.__name__,
            error=str(exc),
            agent_acquire_ms=agent_acquire_ms,
            agent_run_ms=int((perf_counter() - agent_run_started) * 1000) if agent_run_started else 0,
        )
        return BehaviorPlanningFailed(exc)
    messages_json = result.new_messages_json().decode("utf-8") if hasattr(result, "new_messages_json") else ""
    return BehaviorPlanned(result.output, messages_json)


@_builder.step(node_id="persist_planned_response", label="校验并保存行为计划")
async def persist_planned_response(ctx: StepContext[ChatGraphState, ChatGraphDeps, BehaviorPlanned]) -> TurnPersisted:
    """保存 Agent 的行为计划；Godot 校验只过滤不可执行字段，不补写剧情或行为。"""
    ctx.state.agent_messages_json = ctx.inputs.new_messages_json
    response = ctx.deps.validator.finalize_response(ctx.state.request, ctx.inputs.response)
    if _request_is_current(ctx.state, ctx.deps):
        _create_navigation_task(ctx.state.request, ctx.deps.memory_store, response)
    return TurnPersisted(_finish_turn(ctx.state, ctx.deps, response))


@_builder.step(node_id="persist_safe_fallback", label="保存安全降级回复")
async def persist_safe_fallback(ctx: StepContext[ChatGraphState, ChatGraphDeps, BehaviorPlanningFailed]) -> TurnPersisted:
    """模型失败时生成并保存一条不会要求 Godot 执行未知动作的本地回复。"""
    response = ctx.deps.validator.local_fallback_response(ctx.state.request)
    response.fallback = True
    response.error = classify_model_error(ctx.inputs.error).code
    response = ctx.deps.validator.finalize_response(ctx.state.request, response)
    return TurnPersisted(_finish_turn(ctx.state, ctx.deps, response))


@_builder.step(node_id="refresh_session_summary", label="按需压缩较早对话")
async def refresh_session_summary(ctx: StepContext[ChatGraphState, ChatGraphDeps, TurnPersisted]) -> ChatResponse:
    """当未摘要的对话超过窗口两倍时，调用专用 Agent 更新摘要。

    摘要失败不会影响已完成的聊天回合；原始 turns 始终保留，可用于之后重新生成。
    """
    state, deps, response = ctx.state, ctx.deps, ctx.inputs.response
    threshold = max(12, int(deps.settings.context_window_turns) * 2)
    summary, summary_turn_id = deps.memory_store.get_session_summary(state.request.session_id)
    turns = deps.memory_store.get_turns_after(state.request.session_id, summary_turn_id, limit=threshold + 8)
    if len(turns) < threshold or state.resolved_provider is None:
        return response
    transcript = "\n".join(f"{turn.role}: {turn.content}" for turn in turns)
    prompt = "已有摘要：\n%s\n\n需要合并的新对话：\n%s" % (summary or "（无）", transcript)
    try:
        if deps.agent_pool is None:
            summary_agent = build_summary_agent(deps.settings, state.resolved_provider)
        else:
            summary_agent = await deps.agent_pool.get(
                "summary",
                state.resolved_provider,
                lambda: build_summary_agent(deps.settings, state.resolved_provider),
            )
        refreshed = (await summary_agent.run(prompt)).output.strip()
        deps.memory_store.update_session_summary(state.request.session_id, refreshed, turns[-1].id)
    except Exception:
        pass
    return response


# ``decision`` 只负责按上一步的类型分流；行为本身由 PydanticAI Agent 和规划文档决定。
_route_plan = _builder.decision(note="使用成功的行为计划，或保存安全降级回复", node_id="behavior_plan_result")
_route_plan = _route_plan.branch(_builder.match(BehaviorPlanned).to(persist_planned_response))
_route_plan = _route_plan.branch(_builder.match(BehaviorPlanningFailed).to(persist_safe_fallback))
_builder.add(_builder.edge_from(_builder.start_node).to(record_player_turn))
_builder.add(_builder.edge_from(record_player_turn).to(load_context))
_builder.add(_builder.edge_from(load_context).to(retrieve_memory, retrieve_knowledge, retrieve_story_events, fork_id="context_retrieval"))
_builder.add(_builder.edge_from(retrieve_memory, retrieve_knowledge, retrieve_story_events).to(_context_join))
_builder.add(_builder.edge_from(_context_join).to(plan_behavior))
_builder.add(_builder.edge_from(plan_behavior).to(_route_plan))
_builder.add(_builder.edge_from(persist_planned_response, persist_safe_fallback).to(refresh_session_summary))
_builder.add(_builder.edge_from(refresh_session_summary).to(_builder.end_node))
CHAT_GRAPH = _builder.build()


def run_chat_graph(*, request: Any, deps: ChatGraphDeps) -> ChatResponse:
    """给同步测试和脚本使用的图入口；线上 HTTP 请求使用下方异步入口。"""
    return CHAT_GRAPH.run_sync(state=ChatGraphState(request=request), deps=deps)


async def run_chat_graph_async(*, request: Any, deps: ChatGraphDeps) -> ChatResponse:
    """在 FastAPI 主事件循环运行图，使 AgentPool 可以安全复用 HTTP 连接。"""
    return await CHAT_GRAPH.run(state=ChatGraphState(request=request), deps=deps)


def _finish_turn(state: ChatGraphState, deps: ChatGraphDeps, response: ChatResponse) -> ChatResponse:
    """写入记忆和助手回合，再补齐 Godot 需要的会话/分支标识。"""
    # 玩家在模型生成期间改口时，前端会发更高 sequence。旧回合仍可能已经
    # 在上游模型中运行，但不能再写入 assistant turn、记忆或动作任务。
    if not _request_is_current(state, deps):
        response.ok = False
        response.dialogue = ""
        response.action_line = []
        response.memory_updates = []
        response.story_events = []
        response.tts.requested = False
        response.response_kind = "superseded"
        response.superseded = True
        response.error = "superseded_by_newer_request"
        response.session_id = state.request.session_id
        response.client_request_id = str(getattr(state.request, "client_request_id", ""))
        response.client_sequence = int(getattr(state.request, "client_sequence", 0))
        return response

    # 这是协议边界：未请求翻译时，即使模型多填了字段也不向客户端暴露。
    if not bool(getattr(state.request, "generate_japanese", False)):
        response.dialogue_ja = ""
    steering = getattr(state.request, "steering", None)
    steering_mode = str(getattr(steering, "mode", "none") or "none")
    if steering_mode != "none":
        response.response_kind = "steered"
        response.steering_ack = steering.model_dump(mode="json") if hasattr(steering, "model_dump") else {}
    if state.is_tool_result:
        context = state.request.context if isinstance(state.request.context, dict) else {}
        protocol = context.get("godot_tool_result", {}) if isinstance(context.get("godot_tool_result", {}), dict) else {}
        response.response_kind = "godot_tool_result"
        response.tool_call_id = str(protocol.get("tool_call_id", "")).strip()
    response.used_knowledge = state.knowledge
    response.used_memory = state.memory
    response.used_story_events = state.recalled_story_events
    response.memory_updates = _store_memory_updates(deps.memory_store, state.request.session_id, state.memory_updates, state.user_turn_id)
    response.story_events = _store_story_events(deps.memory_store, state.request.session_id, state.story_events, state.user_turn_id)
    payload = response.model_dump(mode="json")
    if state.agent_messages_json:
        payload["agent_messages_json"] = state.agent_messages_json
    assistant_turn = deps.memory_store.add_turn(
        state.request.session_id,
        "assistant",
        response.dialogue,
        payload,
    )
    response.session_id = state.request.session_id
    response.turn_id = int(assistant_turn.id)
    response.client_request_id = str(getattr(state.request, "client_request_id", ""))
    response.client_sequence = int(getattr(state.request, "client_sequence", 0))
    response.forked_from = str(state.fork_info.get("forked_from", ""))
    response.forked_at_turn_id = int(state.fork_info.get("forked_at_turn_id", 0))
    _trace(
        deps,
        "output",
        session_id=state.request.session_id,
        response=response.model_dump(mode="json", exclude={"used_knowledge", "used_memory", "used_story_events"}),
        used_knowledge_sources=[str(hit.get("source", "")) for hit in state.knowledge],
        used_memory_count=len(state.memory),
        used_story_event_count=len(state.recalled_story_events),
        elapsed_ms=int((perf_counter() - state.started_at) * 1000),
    )
    return response


def _request_is_current(state: ChatGraphState, deps: ChatGraphDeps) -> bool:
    coordinator = deps.request_coordinator
    if coordinator is None:
        return True
    try:
        return bool(coordinator.is_current(state.request))
    except Exception:
        # 协调器只是并发保护，不应因为调试组件异常而阻断正常对话。
        return True


def _trace(deps: ChatGraphDeps, event: str, **fields: Any) -> None:
    """记录一行可复制的 Chat 调试信息；请求中的 provider 被输入步骤主动排除。"""
    if not bool(getattr(deps.settings, "chat_trace_enabled", True)):
        return
    _CHAT_LOGGER.info("[ChatTrace] %s %s", event, json.dumps(fields, ensure_ascii=False, default=str))


def _is_godot_tool_result_request(request: Any) -> bool:
    """判断本回合是否由 Godot 工具结果触发，而不是玩家输入触发。"""
    context = getattr(request, "context", None)
    if not isinstance(context, dict):
        return False
    return str(context.get("request_source", "")).strip().lower() in {
        "godot_tool_result",
        "godot_tool",
    }


def _attach_verified_task(request: Any, memory_store: Any) -> Any:
    """把 Godot 的导航结果变成可信上下文，供本回合 Agent 决定后续而非自行猜测。"""
    context = request.context if isinstance(getattr(request, "context", None), dict) else {}
    source = context.get("source_decision", {})
    if not isinstance(source, dict):
        return request
    task_id = str(source.get("task_id", "")).strip()
    event = str(source.get("event", "")).strip()
    if not task_id or not event:
        return request
    target_ref = str(source.get("target_nav_point", "")).strip() or str(source.get("target_object", "")).strip()
    task = memory_store.record_navigation_task_result(
        request.session_id,
        task_id,
        event=event,
        ok=bool(source.get("ok", False)),
        target_ref=target_ref,
    )
    if task is None:
        return request
    updated_context = {**context, "verified_task": task.to_dict()}
    return request.model_copy(update={"context": updated_context})


def _create_navigation_task(request: Any, memory_store: Any, response: ChatResponse) -> None:
    """为动作线首步创建回调标识，并把 task_id 写回该步骤。"""
    current = next((step for step in response.action_line if step.step_id == response.current_step_id), None)
    if current is None:
        current = next((step for step in response.action_line if step.command), None)
    if current is None or current.command not in {"go_to_nav_point", "go_to_object", "pick_up_item", "take_from_container", "use_item", "eat_item", "give_item_to_player"}:
        return
    payload = current.command_payload if isinstance(current.command_payload, dict) else {}
    target_ref = str(payload.get("target_nav_point", "")).strip() or str(payload.get("target_object", "")).strip()
    # 递物品没有空间目标，但仍然是一个需要等待玩家接受/拒绝的异步任务。
    if not target_ref and current.command == "give_item_to_player":
        target_ref = "player"
    if not target_ref:
        return
    task_id = f"task:{uuid4().hex}"
    task = memory_store.create_navigation_task(
        request.session_id,
        task_id=task_id,
        goal=str(request.player_text).strip()[:500],
        command=current.command,
        target_ref=target_ref,
    )
    response.task_id = task.task_id
    # Godot 回传时用同一个 tool_call_id 关联本次首步，便于日志和重试排查。
    current.command_payload = {
        **payload,
        "task_id": task.task_id,
        "tool_call_id": f"call:{task.task_id}:{current.step_id}",
    }
    response.current_step_id = current.step_id


def _agent_message_history(turns: list[Any]) -> list[Any]:
    """优先复用 PydanticAI 原生消息；旧存档没有原生消息时降级为普通文本历史。"""
    restored: list[Any] = []
    for turn in turns:
        raw = turn.payload.get("agent_messages_json", "") if isinstance(getattr(turn, "payload", None), dict) else ""
        if raw:
            try:
                restored.extend(ModelMessagesTypeAdapter.validate_json(raw))
            except Exception:
                continue
    if restored:
        return restored
    history: list[Any] = []
    for turn in turns:
        if turn.role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=turn.content)]))
        elif turn.role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=turn.content)]))
    return history


def _resolve_timeline(request: Any, memory_store: Any, memory_retriever: Any) -> tuple[Any, dict[str, Any]]:
    """若玩家从旧回合继续对话，复制存档分支后再写入新回合。"""
    context = request.context if isinstance(request.context, dict) else {}
    try:
        checkpoint = max(0, int(context.get("ai_checkpoint_turn_id", context.get("checkpoint_turn_id", 0))))
    except (TypeError, ValueError):
        checkpoint = 0
    latest = memory_store.get_latest_turn_id(request.session_id)
    if checkpoint <= 0 or latest <= 0 or checkpoint >= latest:
        return request, {}
    session_id = "%s:branch_%s" % (request.session_id or "default_session", uuid4().hex[:10])
    memory_store.fork_session(request.session_id, checkpoint, session_id)
    if memory_retriever is not None:
        try:
            memory_retriever.clear_session_vectors(session_id)
        except Exception:
            pass
    data = request.model_dump(mode="python")
    data["session_id"] = session_id
    data["context"] = {**context, "forked_from": request.session_id, "forked_at_turn_id": checkpoint, "ai_checkpoint_turn_id": 0}
    return request.__class__(**data), {"forked_from": request.session_id, "forked_at_turn_id": checkpoint}


def _store_memory_updates(memory_store: Any, session_id: str, updates: list[dict[str, Any]], source_turn_id: int) -> list[dict[str, Any]]:
    """将模型和规则提取出的长期记忆写入 SQLite。"""
    stored: list[dict[str, Any]] = []
    for update in updates:
        try:
            fact = memory_store.upsert_memory_fact(
                session_id=session_id,
                subject=str(update.get("subject", "player")),
                predicate=str(update.get("predicate", "related_to")),
                value=str(update.get("value", "")),
                confidence=float(update.get("confidence", 0.75)),
                source_turn_id=source_turn_id,
                kind=str(update.get("kind", _memory_kind(update.get("predicate", "")))),
                importance=float(update.get("importance", 0.75)),
            )
        except (TypeError, ValueError):
            continue
        stored.append(fact.to_dict())
    return stored


def _memory_kind(predicate: Any) -> str:
    """给记忆归类，方便 Agent 在后续回合区分偏好、承诺和身份事实。"""
    value = str(predicate or "").lower()
    if value in {"likes", "dislikes", "preference"}:
        return "preference"
    if value in {"wants", "expedition_target", "goal"}:
        return "goal"
    if value in {"promised", "promise", "will"}:
        return "promise"
    return "identity" if value == "name" else "fact"


def _store_story_events(memory_store: Any, session_id: str, events: list[dict[str, Any]], source_turn_id: int) -> list[dict[str, Any]]:
    """保存 Agent tool 收集到的生活片段，和玩家事实分开存储。"""
    stored: list[dict[str, Any]] = []
    for event in events:
        try:
            stored.append(
                memory_store.add_story_event(
                    session_id,
                    "daily_life",
                    str(event.get("summary", "")),
                    importance=float(event.get("importance", 0.6)),
                    source_turn_id=source_turn_id,
                )
            )
        except (TypeError, ValueError):
            continue
    return stored

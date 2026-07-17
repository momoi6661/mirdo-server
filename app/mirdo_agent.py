"""PydanticAI Agent runtime：Mirdo 对话与外出 GM 共用模型适配，但身份和 tools 分离。

这个文件是“调用 AI”的统一入口：不自己拼 HTTP 请求，也不自己解析 JSON。
"""
from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any

from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pydantic_ai import Agent, PromptedOutput, RunContext
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .config import Settings
from .llm_provider import ResolvedProvider


_AGENT_LOGGER = logging.getLogger("mirdo.agent")


@dataclass
class AgentContext:
    """PydanticAI 通过 ``RunContext`` 注入给 tools 的运行数据。

    记忆工具只把候选内容写入两个列表；Graph 在同一回合结束时统一落库，避免模型 tool
    中途失败留下半条存档。
    """
    request: Any
    rag_retriever: Any
    memory_retriever: Any
    memory_store: Any | None = None
    pending_facts: list[dict[str, Any]] | None = None
    pending_story_events: list[dict[str, Any]] | None = None


# ``Callable[[...], Any]`` 是 Python 的函数类型标注：前三个类型是入参，最后一个是返回值。
# 它让测试可以传入假的 Agent 工厂，而生产环境默认使用 ``build_mirdo_agent``。
AgentFactory = Callable[[Settings, ResolvedProvider, Any], Any]


class AgentPool:
    """复用常驻 Chat Agent 及其官方 OpenAI 客户端连接。

    PydanticAI 的 ``Agent`` 可以在多次 ``run`` 中复用；真正按回合变化的记忆、
    RAG 和玩家输入仍通过 ``RunContext`` 传入。这个池只在 FastAPI 的主事件循环中
    使用，服务关闭时统一关闭每个服务商的客户端连接。
    """

    def __init__(self) -> None:
        self._agents: dict[tuple[str, ResolvedProvider], Agent] = {}
        self._lock = asyncio.Lock()

    async def get(
        self,
        role: str,
        resolved: ResolvedProvider,
        create_agent: Callable[[], Agent],
    ) -> Agent:
        """取得指定角色和服务商的 Agent；缓存未命中时调用 create_agent。"""
        key = (role, resolved)
        async with self._lock:
            agent = self._agents.get(key)
            if agent is None:
                # 这是调用方传入的“创建函数”，不是 Agent.build()。
                agent = create_agent()
                await agent.__aenter__()
                self._agents[key] = agent
            return agent

    async def close(self) -> None:
        """在 FastAPI 停止时关闭 PydanticAI Agent 和其 OpenAI 客户端。"""
        async with self._lock:
            agents = list(self._agents.values())
            self._agents.clear()
        for agent in agents:
            await agent.__aexit__(None, None, None)
            await agent.model.client.close()


def load_behavior_guide(knowledge_dir: Path) -> str:
    """读取并返回给 Agent 的行为规划文档。

    文档放在知识库目录而非 Python 字符串中，方便你在不改代码的情况下调整 Mirdo 的
    行为逻辑。读取失败时返回最小规则，后端仍可安全运行。
    """
    path = knowledge_dir / "mirdo_behavior_planning.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return "每回合只规划一个可执行动作；先说明原因，再说明结果；动作完成后等待 Godot 回调。"


def load_personality_bible(knowledge_dir: Path) -> str:
    """读取不可随查询遗漏的 Mirdo 人格设定，作为稳定 instructions 的一部分。"""
    path = knowledge_dir / "mirdo_personality_bible.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return "你是 Mirdo，称呼玩家为老师；温暖、细致、勇敢，但不莽撞。"


def build_mirdo_agent(settings: Settings, resolved: ResolvedProvider, output_type: Any) -> Agent:
    """构造唯一的运行时 Agent；PydanticAI 负责 tool loop 与结构化输出。"""
    build_started = perf_counter()
    model = _build_openai_chat_model(settings, resolved)
    model_settings = _runtime_model_settings(settings, temperature=settings.temperature)
    output_spec = _structured_output_spec(
        output_type,
        name="submit_response",
        description="Submit the final structured response for Godot after any needed tools.",
    )
    agent = Agent(
        model,
        output_type=output_spec,
        deps_type=AgentContext,
        instructions=_base_instructions(
            load_personality_bible(settings.knowledge_dir),
            load_behavior_guide(settings.knowledge_dir),
        ),
        model_settings=model_settings,
        capabilities=[ProcessHistory(_keep_recent_history(settings.context_window_turns)), _tool_trace_hooks()],
    )

    # 装饰器把这个函数注册给 Agent。每次 Agent.run 时 PydanticAI 会调用它来取得系统指令。
    @agent.instructions
    def mirdo_instructions(ctx: RunContext[AgentContext]) -> str:
        """按事件来源补充本回合规则。

        ``ctx`` 是 PydanticAI 注入的运行上下文。本函数只读取请求来源，不拼接对话、
        记忆或 RAG：前者由 ``message_history`` 承载，后两者由 Graph 传入本次
        ``instructions``。这样每一类上下文都有唯一来源。
        """
        context = getattr(ctx.deps.request, "context", {})
        context = context if isinstance(context, dict) else {}
        source = str(context.get("request_source", "player") or "player")
        source_decision = context.get("source_decision", {})
        steering = getattr(ctx.deps.request, "steering", None)
        steering_mode = str(getattr(steering, "mode", "none") or "none")
        if source == "autonomous":
            turn_rule = "这是 Mirdo 的自主时刻：依据当前资源、感知和冷却状态选择最小且安全的一件小事；不要假装老师下了命令。"
        elif isinstance(source_decision, dict) and source_decision.get("event"):
            turn_rule = "这是 Godot 回传的动作结果：先回应已经发生的结果，再决定停止、汇报或切换到不同的下一步；不要重复刚完成的目标。"
        else:
            turn_rule = "这是老师主动发起的对话：优先回应老师的意图，再决定是否需要一个安全动作。"
        if bool(getattr(ctx.deps.request, "generate_japanese", False)):
            translation_rule = "本回合请求了日语平行字段：先生成自然的中文 dialogue，再把同一含义和情绪翻译到 dialogue_ja；不要在 dialogue_ja 中加入解释或额外剧情。"
        else:
            translation_rule = "本回合没有请求日语平行字段：dialogue_ja 必须留空字符串。"
        if steering_mode != "none":
            steering_rule = (
                "这是玩家对正在进行回合的实时引导。当前 player_text 是最高优先级的最新意图；"
                "不要继续被替代的对白措辞或动作线，也不要向玩家解释请求序号、取消请求等内部机制。"
                "结合 runtime_state 中的 steering 和当前任务，用 task_control 明确选择 continue、pause、replace 或 cancel，"
                "然后像自然被打断的人一样重新回应。"
            )
        else:
            steering_rule = "本回合不是实时引导，按正常对话与任务规则处理。"
        return f"{turn_rule}\n{steering_rule}\n{translation_rule}"

    # ``@agent.tool`` 会把普通 Python 函数暴露成可由模型自行选择调用的工具。
    # 函数签名和 docstring 会被 PydanticAI 转成工具 schema，故无需手写 JSON schema。
    @agent.tool
    def search_knowledge(ctx: RunContext[AgentContext], query: str) -> list[dict[str, Any]]:
        """搜索 Mirdo 的世界、剧情和人格知识库。"""
        clean_query = str(query or "").strip()
        if not clean_query or ctx.deps.rag_retriever is None:
            return []
        return list(ctx.deps.rag_retriever.retrieve(clean_query, top_k=4))

    @agent.tool
    def recall_memory(ctx: RunContext[AgentContext], query: str) -> list[dict[str, Any]]:
        """只检索当前存档会话里已确认的记忆事实。"""
        clean_query = str(query or "").strip()
        if not clean_query:
            return []
        if ctx.deps.memory_retriever is not None:
            return list(ctx.deps.memory_retriever.retrieve(ctx.deps.request.session_id, clean_query, top_k=6))
        if ctx.deps.memory_store is None:
            return []
        return [fact.to_dict() for fact in ctx.deps.memory_store.search_memory_facts(ctx.deps.request.session_id, clean_query, limit=6)]

    @agent.tool
    def recall_story_events(ctx: RunContext[AgentContext], query: str = "") -> list[dict[str, Any]]:
        """回忆已记录的剧情和日常片段；查询时传入简短关键词，而不是整句提问。"""
        if ctx.deps.memory_store is None:
            return []
        events = ctx.deps.memory_store.get_story_events(ctx.deps.request.session_id, limit=20)
        keywords = [part.strip() for part in str(query or "").split() if len(part.strip()) >= 2]
        if not keywords:
            return events[:6]
        return [event for event in events if any(word in str(event.get("summary", "")) for word in keywords)][:6]

    @agent.tool
    def recall_session_summary(ctx: RunContext[AgentContext]) -> str:
        """读取较早对话的压缩摘要；近期原话已在当前 prompt 中。"""
        if ctx.deps.memory_store is None:
            return ""
        summary, _turn_id = ctx.deps.memory_store.get_session_summary(ctx.deps.request.session_id)
        return summary

    @agent.tool
    def remember_fact(
        ctx: RunContext[AgentContext],
        subject: str,
        predicate: str,
        value: str,
        kind: str = "fact",
        importance: float = 0.6,
    ) -> str:
        """记录玩家明确说出的长期事实、偏好、身份或承诺，回合成功后统一保存。"""
        clean_value = str(value or "").strip()
        if not clean_value:
            return "未记录：记忆内容为空。"
        pending = ctx.deps.pending_facts
        if pending is None:
            return "未记录：当前记忆不可用。"
        candidate = {
            "subject": str(subject or "player").strip() or "player",
            "predicate": str(predicate or "related_to").strip() or "related_to",
            "value": clean_value[:80],
            "kind": str(kind or "fact").strip() or "fact",
            "importance": max(0.0, min(float(importance), 1.0)),
            "confidence": 0.85,
        }
        if candidate not in pending:
            pending.append(candidate)
        return "已加入本回合待保存记忆。"

    @agent.tool
    def record_story_event(ctx: RunContext[AgentContext], summary: str, importance: float = 0.6) -> str:
        """记录已经发生且将来值得 Mirdo 回忆的剧情或日常片段。"""
        clean_summary = str(summary or "").strip()
        if not clean_summary:
            return "未记录：事件内容为空。"
        pending = ctx.deps.pending_story_events
        if pending is None:
            return "未记录：当前剧情记忆不可用。"
        candidate = {"summary": clean_summary[:500], "importance": max(0.0, min(float(importance), 1.0))}
        if candidate not in pending:
            pending.append(candidate)
        return "已加入本回合待保存事件。"

    @agent.tool
    def available_actions(ctx: RunContext[AgentContext]) -> dict[str, Any]:
        """读取 Godot 当前允许的动作、语义实体和导航能力。

        这个 tool 是 Server 端 Agent 规划动作链的入口：模型必须用这里返回的
        pickable/consumable 信息决定 `use_item`、`eat_item` 或 `go_to_object`。
        Godot 只执行 action_line 的当前步骤，不替模型猜“去了以后还要不要喝水”。
        """
        context = getattr(ctx.deps.request, "context", {})
        context = context if isinstance(context, dict) else {}
        npc = context.get("npc", {}) if isinstance(context.get("npc", {}), dict) else {}
        perception = context.get("perception", {}) if isinstance(context.get("perception", {}), dict) else {}
        objects = _visible_actionable_objects(perception)
        catalog = context.get("navigation_catalog", context.get("known_nav_points", context.get("ai_nav_points", [])))
        return {
            "commands": ["go_to_object", "go_to_nav_point", "sit_down", "follow_player", "stop_follow", "look_at_player", "pick_up_item", "take_from_container", "use_item", "eat_item", "give_item_to_player"],
            "actions": npc.get("available_body_actions", []),
            "objects": objects,
            "consumables": _visible_consumables(objects),
            "navigation_catalog": catalog,
            "action_line_contract": "Return 0-4 causal steps. Godot executes only the first pending step and reports its real result before the next turn.",
            "rule": "Use go_to_object with target_ref/entity id and an affordance such as open, take_item, sit, or inspect. Use go_to_nav_point only for a waypoint. Godot resolves navigation and Marker roles locally. Visible world items use pick_up_item/use_item/eat_item. An item stored inside a container must use take_from_container with target_object and item_id; Godot navigates first and decrements real inventory. Only after a successful take may the next turn use give_item_to_player.",
        }

    _AGENT_LOGGER.info(
        "[AgentTiming] build_ms=%d model=%s",
        int((perf_counter() - build_started) * 1000),
        resolved.model,
    )
    return agent


def build_expedition_agent(settings: Settings, resolved: ResolvedProvider, output_type: Any) -> Agent:
    """构造“外出 GM” Agent；它叙述主角的探索，不扮演 Mirdo。

    外出与 Mirdo 对话是两个不同的叙事主体：这里的 ``Agent`` 只负责根据世界规则、
    历史事件和玩家关注点生成下一段连续故事。结构化结果仍由 PydanticAI 校验，
    记忆工具只读，实际保存由外出编排器在回合成功后完成。
    """
    model = _build_openai_chat_model(settings, resolved)
    model_settings = _runtime_model_settings(settings, temperature=min(settings.temperature, 0.7))
    output_spec = _structured_output_spec(
        output_type,
        name="submit_expedition",
        description="Submit the final structured expedition result after checking continuity and world constraints.",
    )
    agent = Agent(
        model,
        output_type=output_spec,
        deps_type=AgentContext,
        instructions=(
            "你是避难所游戏的 GM（游戏主持人）和叙事导演，不是 Mirdo，也不要以 Mirdo 的口吻说话。"
            "外出主体是玩家/主角；你负责根据可用地图、资源、风险和已保存的剧情状态，"
            "推动一次有因果的探索，并给下一次外出留下可以继续的线索。"
            "先检查已有故事标记和玩家明确表达过的寻找目标，再决定本次发现。"
            "已解决的线索不要无理由复活；同一地点优先延续 active 的 continuity_key。"
            "不要编造 available_loot 之外的可领取物品，不要把推测写成已经发生的事实。"
            "story 用中文写主角经历，summary 简洁说明原因和后果；如果有未完线索，"
            "用 story_markers 标记 active，并填写 next_hooks。所有输出必须通过结构化结果提交。"
        ),
        model_settings=model_settings,
        capabilities=[ProcessHistory(_keep_recent_history(settings.context_window_turns)), _tool_trace_hooks()],
    )

    @agent.tool
    def recall_expedition_story(ctx: RunContext[AgentContext], query: str = "") -> list[dict[str, Any]]:
        """读取本存档以前的外出故事和未完成线索。"""
        store = ctx.deps.memory_store
        if store is None:
            return []
        events = store.get_story_events(ctx.deps.request.session_id, limit=30)
        clean_query = str(query or "").strip()
        if not clean_query:
            return events[:10]
        terms = [part for part in clean_query.split() if part]
        return [event for event in events if any(term in str(event) for term in terms)][:10]

    @agent.tool
    def recall_expedition_memory(ctx: RunContext[AgentContext], query: str = "") -> list[dict[str, Any]]:
        """读取玩家曾明确说过的偏好和寻找目标，避免每次外出都重新猜测。"""
        store = ctx.deps.memory_store
        if store is None:
            return []
        facts = store.get_memory_facts(ctx.deps.request.session_id, limit=50)
        clean_query = str(query or "").strip()
        if not clean_query:
            return [fact.to_dict() for fact in facts if fact.predicate in {"wants", "likes", "dislikes", "note"}]
        return [fact.to_dict() for fact in facts if clean_query in fact.value or clean_query in fact.predicate]

    @agent.tool
    def search_expedition_knowledge(ctx: RunContext[AgentContext], query: str) -> list[dict[str, Any]]:
        """检索外出规则、地点资料和世界设定；没有命中时必须保守叙述。"""
        retriever = ctx.deps.rag_retriever
        if retriever is None or not str(query or "").strip():
            return []
        return list(retriever.retrieve(str(query).strip(), top_k=6))

    return agent


def _visible_actionable_objects(perception: dict[str, Any]) -> list[dict[str, Any]]:
    """把 Godot 感知中的 nearby/visible 目标合并去重，供 available_actions tool 返回。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in ("nearby_objects", "visible_items"):
        values = perception.get(group, [])
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            object_id = str(item.get("id", "")).strip()
            if not object_id or object_id in seen:
                continue
            seen.add(object_id)
            out.append(item)
    return out


def _visible_consumables(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """提取可拿可用的水/食物。

    这里不替模型决定行为，只把“哪些物体可以喝/吃”整理清楚，减少模型把
    `go_to_object` 错当成“去拿水喝”的概率。
    """
    consumables: list[dict[str, Any]] = []
    for item in objects:
        tags = {str(tag).lower() for tag in item.get("tags", [])} if isinstance(item.get("tags", []), list) else set()
        actions = {str(action).lower() for action in item.get("actions", [])} if isinstance(item.get("actions", []), list) else set()
        text = " ".join([str(item.get("id", "")), str(item.get("name", "")), str(item.get("description", "")), " ".join(tags)]).lower()
        is_pickable = "pickable" in tags or "pick_up" in actions
        is_usable = "usable" in tags or "use" in actions or "eat_if_food" in actions
        if not is_pickable or not is_usable:
            continue
        kind = "water" if any(word in text for word in ("water", "水", "瓶装水", "drink")) else "food" if any(word in text for word in ("food", "食物", "罐头", "吃", "consumable")) else "item"
        if kind not in {"water", "food"}:
            continue
        consumables.append({
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "kind": kind,
            "distance": item.get("distance", ""),
            "recommended_command": "use_item" if kind == "water" else "eat_item",
            "description": item.get("description", ""),
        })
    return consumables


def _keep_recent_history(context_window_turns: int):
    """给 PydanticAI 的 ``ProcessHistory`` 使用：旧内容已有 SQLite 摘要时，只保留最近原生消息。"""
    message_limit = max(4, int(context_window_turns) * 2)

    def process(messages: list[ModelMessage]) -> list[ModelMessage]:
        return list(messages[-message_limit:]) if len(messages) > message_limit else messages

    return process


def _tool_trace_hooks() -> Hooks:
    """注册 PydanticAI 原生 hooks，把工具边界的简短状态打印到服务终端。"""
    hooks = Hooks()

    @hooks.on.before_tool_execute
    def log_tool_start(ctx: RunContext[AgentContext], *, call: Any, tool_def: Any, args: Any) -> Any:
        session_id = getattr(ctx.deps.request, "session_id", "")
        tool_name = getattr(call, "tool_name", getattr(tool_def, "name", "tool"))
        _AGENT_LOGGER.info("[AgentTool] start session=%s tool=%s args=%s", session_id, tool_name, str(args)[:240])
        return args

    @hooks.on.after_tool_execute
    def log_tool_finish(ctx: RunContext[AgentContext], *, call: Any, tool_def: Any, args: Any, result: Any) -> Any:
        session_id = getattr(ctx.deps.request, "session_id", "")
        tool_name = getattr(call, "tool_name", getattr(tool_def, "name", "tool"))
        size = len(result) if isinstance(result, (dict, list, tuple, str)) else 0
        _AGENT_LOGGER.info("[AgentTool] finish session=%s tool=%s result_type=%s size=%d", session_id, tool_name, type(result).__name__, size)
        return result

    return hooks


def build_probe_agent(settings: Settings, resolved: ResolvedProvider) -> Agent:
    """健康检查也走 PydanticAI，避免为探测再保留一套底层 HTTP 实现。"""
    model = _build_openai_chat_model(settings, resolved, timeout=min(settings.request_timeout, 20))
    model_settings = _runtime_model_settings(settings)
    return Agent(model, output_type=str, instructions="只回复：pong。", model_settings=model_settings)


def _base_instructions(personality_bible: str, behavior_guide: str) -> str:
    """返回稳定的人格与行为规则；静态文档不会因本回合 RAG 未命中而丢失。"""
    return "\n".join(
        [
            "以下是不可违背的 Mirdo 人格设定：",
            personality_bible,
            "不得编造记忆、观察结果、世界事实、目标或其他主角。",
            "当前 day、时间、数值、感知和 Godot 事件已经在 runtime_state 中；普通对话不要为重复读取它们调用 tool。",
            "仅当回答依赖于近期消息和 runtime_state 中不存在的已确认事实时才调用 tool：事实和偏好用 recall_memory；早期共同经历先用 recall_session_summary 或 recall_story_events；剧情和设定用 search_knowledge。不要为当前回合或近期消息已经给出的内容重复调用记忆 tool。",
            "只有玩家明确陈述的长期事实、承诺或已完成事件才可以调用 remember_fact；只把已经发生且值得未来回忆的日常片段写入 record_story_event。",
            "不能因为猜测、计划或普通寒暄创建记忆。",
            "下达移动或交互命令前先调用 available_actions；优先使用 navigation_catalog 中的 entity id 和 affordance，不要猜测坐标或 Marker 路径。",
            "为本回合生成 action_line：0 步表示只对白，1 到 4 步表示有因果关系的计划。每一步包含 step_id、reason、command、command_payload、expected_result；只有首个步骤会立刻交给 Godot，其余步骤必须等待 Godot 的真实结果。",
            "如果 runtime_state 表明存在未完成任务，必须填写 task_control：mode=continue 表示只是回应/引导后继续原任务；mode=pause 表示先处理老师的临时问题，回复后恢复原任务；mode=replace 表示老师给了新的明确目标，用新的 action_line 替换旧任务；mode=cancel 只用于老师明确要求停止/取消。没有当前任务时使用 mode=none。不要因为普通寒暄或一个问题就取消任务。",
            "当 mode=continue 或 pause 且没有新的 action_line 时，不要重复规划旧动作；让 Godot 保留当前任务并在对白完成后恢复。mode=replace/cancel 必须在 dialogue 中说明任务变化的原因。",
            "action_line 的首步必须是当前可执行的 command；若目标不在 navigation_catalog/perception 中，不要编造 id，也不要把后续步骤提前执行。",
            "如果 Mirdo 口渴且只知道水在柜子里，先规划 go_to_object 到可见的柜子；到达后由 Godot 回传观察，再规划寻找/拿取/饮用。若已经看见可拿的水，才规划 use_item。",
            "如果老师要求从柜子/箱子拿东西或递给老师：抵达容器后使用 take_from_container(target_object, item_id)，等 Godot 确认库存已减少，再使用 give_item_to_player(item_id)。不得只播放拿取动画，也不得在拿取成功前声称已经递出。",
            "如果 runtime_state 中存在 godot_event，先把它当作 Godot 已确认的动作结果：说明已经发生的事实和观察，再只选择一个后续动作、询问或结束任务。",
            "先说明已经观察到的原因，再说明动作线首步的后果或建议；后续步骤写成条件式计划，不要假装它们已经发生。",
            "每回合必须根据当前事件、老师的语气、Mirdo 的需求和关系状态选择 emotion 与 emotion_intensity；不要无理由总是返回平静。允许的 emotion 包括：平静、温柔、开心、害羞、惊讶、担心、疲惫、生气、安心、期待、疑惑、紧张、害怕、难过、委屈。emotion_intensity 必须是 0.0 到 1.0 的数值；普通回应通常 0.35 到 0.65，危险、重逢或强烈情绪才使用 0.75 以上。",
            "对白要给 TTS 留出自然韵律：疑问使用问号，惊喜或强烈反应使用感叹号，犹豫和害怕可以使用省略号；不要添加会被念出来的情绪标签或舞台说明。",
            "主对白 dialogue 使用中文。只有当运行时明确要求 generate_japanese=true 时，才同时填写 dialogue_ja；否则 dialogue_ja 必须为空。",
            "以下是必须遵循的行为规划文档：",
            behavior_guide,
        ]
    )


def _runtime_model_settings(
    settings: Settings,
    *,
    temperature: float | None = None,
) -> dict[str, Any]:
    """生成跨服务商的 PydanticAI 模型参数。

    最终结构化输出统一走 ``PromptedOutput`` 后，不再需要按服务商切换输出模式。
    这里只保留温度、最大 token 等真正通用的运行参数，避免隐藏的模型特殊分支。
    """
    model_settings: dict[str, Any] = {}
    if temperature is not None:
        model_settings["temperature"] = temperature
    if settings.chat_max_tokens:
        model_settings["max_tokens"] = settings.chat_max_tokens
    return model_settings


def _structured_output_spec(
    output_type: Any,
    *,
    name: str,
    description: str,
) -> Any:
    """统一使用 PydanticAI 的 PromptedOutput 生成结构化结果。

    ``PromptedOutput`` 会把 ``output_type`` 这个 Pydantic 模型转换成 JSON
    Schema，并通过 ``template`` 注入到模型提示词中。这样所有服务商都走同一套
    “直接输出 JSON -> PydanticAI 校验成模型对象”的流程，避免按服务商分支。

    注意：这只改变“最终答案”的提交方式；业务 tools 仍然由 PydanticAI 作为
    function tools 提供给模型，模型需要记忆、知识库或动作上下文时仍可调用。
    """
    return PromptedOutput(
        output_type,
        name=name,
        description=description,
        template=(
            "Return only one JSON object matching this schema. "
            "Do not use markdown. Do not add explanations.\n{schema}"
        ),
    )


def build_summary_agent(settings: Settings, resolved: ResolvedProvider) -> Agent:
    """构造专门压缩旧对话的 PydanticAI Agent，不参与角色行为和 tools。"""
    model = _build_openai_chat_model(settings, resolved)
    model_settings = _runtime_model_settings(settings, temperature=0.1)
    return Agent(
        model,
        output_type=str,
        instructions="用中文把旧对话压缩为不超过 300 字的连续记忆。保留已确认的事实、承诺、完成事件、未完成目标和人物情绪变化；删除寒暄、猜测和重复。不要编造任何内容。",
        model_settings=model_settings,
    )


def _build_openai_chat_model(
    settings: Settings,
    resolved: ResolvedProvider,
    *,
    timeout: float | None = None,
) -> OpenAIChatModel:
    """按 PydanticAI 官方的 ``OpenAIChatModel + OpenAIProvider`` 方式定义兼容模型。

    ``AsyncOpenAI`` 是官方建议的自定义客户端入口，用来保留项目原有的超时和可选
    代理配置；没有代理时不自行创建 ``httpx.AsyncClient``。所有 OpenAI-compatible
    服务商只需提供 ``base_url``、``api_key`` 与 ``model``。
    """
    effective_timeout = settings.request_timeout if timeout is None else timeout
    client_options: dict[str, Any] = {
        "base_url": resolved.base_url,
        "api_key": resolved.api_key or "not-needed",
        "timeout": effective_timeout,
        # 游戏对话宁可尽快走 Graph 的安全降级，也不让 SDK 在一次超时后阻塞多轮重试。
        "max_retries": 0,
    }
    if resolved.proxy_url:
        client_options["http_client"] = DefaultAsyncHttpxClient(
            proxy=resolved.proxy_url,
            timeout=effective_timeout,
            trust_env=False,
        )
    client = AsyncOpenAI(**client_options)
    return OpenAIChatModel(
        resolved.model,
        provider=OpenAIProvider(openai_client=client),
    )

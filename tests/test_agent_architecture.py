import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ModelRequest, UserPromptPart

from app.chat_orchestrator import ChatOrchestrator, ChatRequestCoordinator
from app.agent_graphs import CHAT_GRAPH, _create_navigation_task
from app.config import Settings
from app.expedition_orchestrator import ExpeditionOrchestrator
from app.llm_provider import LLMProvider, ResolvedProvider
from app.main import create_app
from app.memory.store import MemoryStore
from app.memory.retriever import MemoryRAGRetriever
from app.mirdo_agent import AgentPool, build_mirdo_agent, build_probe_agent, build_summary_agent, load_behavior_guide, load_personality_bible
from app.prompt_builder import PromptBuilder
from app.schemas import ActionStep, ChatRequest, ChatResponse, ExpeditionRequest, ExpeditionResponse, ProviderConfig


class FakeAgent:
    """测试替身模拟 PydanticAI 的 ``Agent.run(...).output`` 约定。"""

    def __init__(self, output):
        self.output = output
        self.prompts: list[str] = []
        self.runtime_instructions: list[str] = []

    async def run(self, prompt: str, *, deps, **kwargs):
        self.prompts.append(prompt)
        self.runtime_instructions.append(str(kwargs.get("instructions", "")))
        return type("AgentRunResult", (), {"output": self.output})()

    def run_sync(self, prompt: str, *, deps, **_):
        self.prompts.append(prompt)
        return type("AgentRunResult", (), {"output": self.output})()


class FailingAgent:
    """让图实际走 AgentFailed → use_safe_fallback 分支。"""

    async def run(self, prompt: str, *, deps, **_):
        raise RuntimeError("upstream unavailable")


class RememberingAgent:
    """模拟 Agent 通过记忆 tools 把候选事实交给 Graph 统一保存。"""

    async def run(self, prompt: str, *, deps, **_):
        deps.pending_facts.append({"subject": "player", "predicate": "likes", "value": "热可可", "kind": "preference", "importance": 0.8})
        deps.pending_story_events.append({"summary": "老师在雨夜带回热可可，Mirdo 很开心。", "importance": 0.7})
        return type("AgentRunResult", (), {"output": ChatResponse(dialogue="老师，热可可暖起来啦。")})()


class ContextInspectingAgent:
    """确认 Graph 合并后的三类上下文作为本回合 instructions 传入 Agent。"""

    def __init__(self):
        self.runtime_instructions = ""

    async def run(self, prompt: str, *, deps, **kwargs):
        self.runtime_instructions = str(kwargs.get("instructions", ""))
        return type("AgentRunResult", (), {"output": ChatResponse(dialogue="老师，我记得这件事。")})()


class SequencedAgent:
    """按回合返回预设输出，并保留每回合拿到的运行时上下文。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.runtime_instructions: list[str] = []

    async def run(self, _prompt: str, *, deps, **kwargs):
        self.runtime_instructions.append(str(kwargs.get("instructions", "")))
        return type("AgentRunResult", (), {"output": self.outputs.pop(0)})()


class StaticRetriever:
    def retrieve(self, _query: str, _top_k: int = 4):
        return [{"source": "mirdo_story_bible.md", "text": "Mirdo 会记得老师平安回家。"}]


class MixedRetriever:
    """模拟检索结果混入已由 Agent 固定加载的人格/行为文档。"""

    def retrieve(self, _query: str, _top_k: int = 4):
        return [
            {"source": "mirdo_personality_bible.md", "text": "固定人格，不应重复塞入运行时上下文。"},
            {"source": "mirdo_behavior_planning.md", "text": "固定行为规则，不应重复塞入运行时上下文。"},
            {"source": "mirdo_story_bible.md", "text": "Mirdo 会记得老师平安回家。"},
        ]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversation.sqlite3",
        rag_db=tmp_path / "rag.sqlite3",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="http://localhost:11434/v1",
        chat_model="test-model",
    )


def test_chat_uses_typed_agent_output_and_graph(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    fake = FakeAgent(ChatResponse(dialogue="老师，先确认了走廊很安静，我们再去看物资柜。", action="walk"))
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=LLMProvider(settings),
        agent_factory=lambda *_args: fake,
    )

    response = orchestrator.chat(ChatRequest(session_id="agent", player_text="看看物资柜"))

    assert response.dialogue.startswith("老师")
    assert response.fallback is False
    assert fake.prompts[0] == "看看物资柜"
    assert "<runtime_state>" in fake.runtime_instructions[0]
    assert "Return JSON" not in fake.runtime_instructions[0]


def test_realtime_steering_restarts_agent_with_clean_player_text(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    fake = FakeAgent(ChatResponse(dialogue="好，我先停下，听老师的新安排。"))
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=LLMProvider(settings),
        agent_factory=lambda *_args: fake,
    )

    response = orchestrator.chat(
        ChatRequest(
            session_id="steer",
            player_text="等等，先不要过去",
            client_request_id="request-2",
            client_sequence=2,
            supersedes_request_id="request-1",
            steering={
                "mode": "interrupt",
                "phase": "presentation",
                "target_request_id": "request-1",
                "target_client_sequence": 1,
                "interrupted_dialogue": "老师，我现在去门口看看。",
                "reason": "player_guidance",
            },
        )
    )

    assert fake.prompts == ["等等，先不要过去"]
    assert '"mode":"interrupt"' in fake.runtime_instructions[0]
    assert response.response_kind == "steered"
    assert response.steering_ack["target_request_id"] == "request-1"


def test_request_coordinator_marks_older_generation_as_stale():
    coordinator = ChatRequestCoordinator()
    old = ChatRequest(session_id="steer", player_text="去门口", client_request_id="r1", client_sequence=1)
    latest = ChatRequest(
        session_id="steer",
        player_text="等等，先别去",
        client_request_id="r2",
        client_sequence=2,
        supersedes_request_id="r1",
        steering={"mode": "interrupt", "phase": "generation", "target_request_id": "r1"},
    )

    coordinator.register(old)
    coordinator.register(latest)

    assert coordinator.is_current(old) is False
    assert coordinator.is_current(latest) is True


def test_chat_route_uses_the_async_graph_entrypoint(tmp_path: Path):
    settings = _settings(tmp_path)
    fake = FakeAgent(ChatResponse(dialogue="老师，我已经听见你的安排了。"))
    app = create_app(settings, agent_factory=lambda *_args: fake)

    with TestClient(app) as client:
        response = client.post("/chat", json={"session_id": "route", "player_text": "先休息一下"})

    assert response.status_code == 200
    assert response.json()["dialogue"] == "老师，我已经听见你的安排了。"
    assert "[ChatTrace]" in (settings.runtime_dir / "server.log").read_text(encoding="utf-8")


def test_godot_action_result_route_reenters_graph_without_fake_user_turn(tmp_path: Path):
    """工具结果应触发同一 Graph，但历史里不能多出一条伪造的玩家消息。"""
    settings = _settings(tmp_path)
    fake = FakeAgent(ChatResponse(dialogue="老师，我已经确认动作结果了，接下来先观察一下。"))
    app = create_app(settings, agent_factory=lambda *_args: fake)

    with TestClient(app) as client:
        response = client.post(
            "/godot/action-result",
            json={
                "session_id": "tool-route",
                "tool_call_id": "tool:arrival:1",
                "task_id": "task:missing-is-safe",
                "step_id": "step:1",
                "event": "navigation_goal_finished",
                "status": "succeeded",
                "ok": True,
                "action_result": {"ok": True, "target_ref": "water_cabinet"},
                "observation": {"nearby": ["water_cabinet"]},
            },
        )
        history = client.get("/session/tool-route/history").json()["turns"]

    assert response.status_code == 200
    assert response.json()["response_kind"] == "godot_tool_result"
    assert response.json()["tool_call_id"] == "tool:arrival:1"
    assert len(history) == 1
    assert history[0]["role"] == "assistant"


def test_chat_trace_logs_redacted_input_and_full_response(tmp_path: Path, caplog):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=LLMProvider(settings),
        agent_factory=lambda *_args: FakeAgent(ChatResponse(dialogue="老师，物资柜很安静。")),
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    response = orchestrator.chat(
        ChatRequest(
            session_id="trace",
            player_text="看看物资柜",
            provider=ProviderConfig(api_key="must-not-appear", model="test-model"),
        )
    )

    trace = "\n".join(record.getMessage() for record in caplog.records if "[ChatTrace]" in record.getMessage())
    assert "[ChatTrace] input" in trace
    assert "看看物资柜" in trace
    assert "[ChatTrace] output" in trace
    assert response.dialogue in trace
    assert "must-not-appear" not in trace
    assert '"elapsed_ms"' in trace


def test_expedition_uses_typed_agent_output_without_json_parser(tmp_path: Path):
    settings = _settings(tmp_path)
    fake = FakeAgent(ExpeditionResponse(summary="老师，我们确认退路后带着补给回来了。"))
    orchestrator = ExpeditionOrchestrator(
        settings=settings,
        llm_provider=LLMProvider(settings),
        agent_factory=lambda *_args: fake,
    )

    response = orchestrator.resolve(ExpeditionRequest(location={"name": "旧药店"}))

    assert response.summary.startswith("老师")
    assert response.fallback is False
    assert "available_loot" in fake.prompts[0]


def test_graph_decision_routes_agent_failure_to_safe_fallback(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=LLMProvider(settings),
        agent_factory=lambda *_args: FailingAgent(),
    )

    response = orchestrator.chat(ChatRequest(player_text="现在怎么样？"))

    assert response.fallback is True
    assert response.error == "model_call_failed"


def test_graph_persists_agent_tool_memories_and_story_events(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=LLMProvider(settings),
        agent_factory=lambda *_args: RememberingAgent(),
    )

    response = orchestrator.chat(ChatRequest(session_id="life", player_text="我带回热可可了"))

    assert response.memory_updates[0]["value"] == "热可可"
    assert response.story_events[0]["summary"].startswith("老师在雨夜")
    assert store.search_memory_facts("life", "喜欢什么", 1)[0].value == "热可可"
    assert store.get_story_events("life")[0]["summary"].startswith("老师在雨夜")


def test_graph_injects_memory_story_and_knowledge_context_into_agent(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    store.upsert_memory_fact("context", "player", "likes", "热可可")
    store.add_story_event("context", "daily_life", "老师和 Mirdo 一起修好了收音机")
    agent = ContextInspectingAgent()
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=LLMProvider(settings),
        rag_retriever=StaticRetriever(),
        memory_retriever=MemoryRAGRetriever(memory_store=store, settings=settings),
        agent_factory=lambda *_args: agent,
    )

    response = orchestrator.chat(ChatRequest(session_id="context", player_text="你还记得什么？"))

    assert "热可可" in agent.runtime_instructions
    assert "mirdo_story_bible.md" in agent.runtime_instructions
    assert "收音机" in agent.runtime_instructions
    assert response.used_story_events[0]["summary"].endswith("收音机")


def test_graph_does_not_repeat_static_agent_documents_in_runtime_rag(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    agent = ContextInspectingAgent()
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=LLMProvider(settings),
        rag_retriever=MixedRetriever(),
        agent_factory=lambda *_args: agent,
    )

    response = orchestrator.chat(ChatRequest(session_id="static-docs", player_text="你还记得回家的事吗？"))

    assert "mirdo_story_bible.md" in agent.runtime_instructions
    assert "mirdo_personality_bible.md" not in agent.runtime_instructions
    assert "mirdo_behavior_planning.md" not in agent.runtime_instructions
    assert [hit["source"] for hit in response.used_knowledge] == ["mirdo_story_bible.md"]


def test_graph_persists_verified_navigation_result_for_next_agent_turn(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    agent = SequencedAgent(
        [
            ChatResponse(
                dialogue="老师，我去卫生间看看。",
                action_line=[ActionStep(step_id="toilet", command="go_to_nav_point", command_payload={"target_nav_point": "toilet_look_point"})],
                task_status="continue",
            ),
            ChatResponse(dialogue="老师，我已经到卫生间了。要先看镜子还是洗手台？"),
        ]
    )
    orchestrator = ChatOrchestrator(
        settings=settings,
        memory_store=store,
        llm_provider=LLMProvider(settings),
        agent_factory=lambda *_args: agent,
    )
    nav_context = {"known_nav_points": [{"id": "toilet_look_point", "name": "卫生间"}]}

    first = orchestrator.chat(ChatRequest(session_id="task-loop", player_text="去卫生间看看", context=nav_context))
    task_id = first.task_id
    second = orchestrator.chat(
        ChatRequest(
            session_id="task-loop",
            player_text="Mirdo 已到达目标，等待你根据结果继续。",
            context={
                **nav_context,
                "request_source": "autonomous",
                "source_decision": {
                    "event": "navigation_goal_finished",
                    "task_id": task_id,
                    "ok": True,
                    "target_nav_point": "toilet_look_point",
                },
            },
        )
    )

    assert task_id
    assert first.action_line[0].command_payload["task_id"] == task_id
    assert "status=succeeded" in agent.runtime_instructions[1]
    assert "event=navigation_goal_finished" in agent.runtime_instructions[1]
    assert "卫生间" in second.dialogue


def test_pickup_command_waits_for_godot_result(tmp_path: Path):
    settings = _settings(tmp_path)
    store = MemoryStore(settings.conversation_db)
    store.initialize()
    response = ChatResponse(
        dialogue="老师，我把绷带拿起来看看。",
        action_line=[ActionStep(step_id="pickup", command="pick_up_item", command_payload={"target_object": "bandage"})],
        task_status="continue",
    )

    _create_navigation_task(ChatRequest(session_id="pickup-loop", player_text="拿起绷带"), store, response)

    assert response.task_id
    assert response.action_line[0].command_payload["task_id"] == response.task_id


def test_gift_command_also_gets_task_id_for_acceptance_event(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.db")
    store.initialize()
    response = ChatResponse(
        dialogue="老师，这瓶水给你。",
        action_line=[ActionStep(step_id="give-water", command="give_item_to_player", command_payload={"item_id": "water_bottle"})],
        task_status="continue",
    )
    _create_navigation_task(ChatRequest(session_id="gift-loop", player_text="把水递给我"), store, response)
    assert response.task_id.startswith("task:")
    assert response.action_line[0].command_payload["task_id"] == response.task_id


def test_navigation_task_is_attached_to_the_first_action_line_step(tmp_path: Path):
    """Graph 创建导航任务时，把 task 元数据写回动作线首步，供 Godot 回传进度。"""
    store = MemoryStore(tmp_path / "memory.db")
    store.initialize()
    response = ChatResponse(
        dialogue="老师，我先去拿水。",
        action_line=[
            ActionStep(
                step_id="go-to-water",
                command="use_item",
                command_payload={"target_object": "water_bottle"},
                reason="到达后先拿起再喝",
            ),
            ActionStep(step_id="report-thirst", command="", reason="喝完告诉老师感觉"),
        ],
    )
    _create_navigation_task(ChatRequest(session_id="line-loop", player_text="我口渴了"), store, response)
    assert response.task_id.startswith("task:")
    assert response.current_step_id == "go-to-water"
    assert response.action_line[0].command_payload["task_id"] == response.task_id


def test_provider_only_resolves_connection_configuration(tmp_path: Path):
    provider = LLMProvider(_settings(tmp_path))
    assert provider.resolve_provider(ProviderConfig(base_url="http://localhost:11434/v1", api_key="test-key", model="test-model")) == ResolvedProvider(
        base_url="http://localhost:11434/v1", api_key="test-key", model="test-model", proxy_url=""
    )
    assert not hasattr(provider, "build_chat_model")



def test_provider_adds_v1_for_openai_compatible_root_url(tmp_path: Path):
    provider = LLMProvider(_settings(tmp_path))

    resolved = provider.resolve_provider(ProviderConfig(base_url="http://127.0.0.1:8317", api_key="test-key", model="grok-4.5"))

    assert resolved.base_url == "http://127.0.0.1:8317/v1"



def test_provider_ignores_proxy_for_local_openai_gateway(tmp_path: Path):
    provider = LLMProvider(_settings(tmp_path))

    resolved = provider.resolve_provider(ProviderConfig(base_url="http://127.0.0.1:8317", api_key="test-key", model="grok-4.5", proxy_url="http://127.0.0.1:7890"))

    assert resolved.base_url == "http://127.0.0.1:8317/v1"
    assert resolved.proxy_url == ""


def test_agents_omit_the_output_limit_when_configured_as_zero(tmp_path: Path):
    settings = _settings(tmp_path).model_copy(update={"chat_max_tokens": 0})
    provider = ResolvedProvider(base_url="http://localhost:11434/v1", api_key="test-key", model="test-model")

    assert "max_tokens" not in build_mirdo_agent(settings, provider, ChatResponse).model_settings
    assert "max_tokens" not in build_probe_agent(settings, provider).model_settings
    assert "max_tokens" not in build_summary_agent(settings, provider).model_settings


def test_agent_disables_slow_sdk_transport_retries(tmp_path: Path):
    settings = _settings(tmp_path)
    provider = ResolvedProvider(base_url="http://localhost:11434/v1", api_key="test-key", model="test-model")

    assert build_mirdo_agent(settings, provider, ChatResponse).model.client.max_retries == 0


def test_mirdo_agent_uses_pydantic_process_history_for_old_messages(tmp_path: Path):
    settings = _settings(tmp_path).model_copy(update={"context_window_turns": 3})
    provider = ResolvedProvider(base_url="http://localhost:11434/v1", api_key="test-key", model="test-model")
    agent = build_mirdo_agent(settings, provider, ChatResponse)
    processor = next(capability for capability in agent.root_capability.capabilities if isinstance(capability, ProcessHistory))
    history = [ModelRequest(parts=[UserPromptPart(content=str(index))]) for index in range(10)]

    assert processor.processor(history) == history[-6:]


def test_mirdo_agent_uses_pydantic_hooks_for_tool_observability(tmp_path: Path):
    settings = _settings(tmp_path)
    provider = ResolvedProvider(base_url="http://localhost:11434/v1", api_key="test-key", model="test-model")
    agent = build_mirdo_agent(settings, provider, ChatResponse)

    assert any(isinstance(capability, Hooks) for capability in agent.root_capability.capabilities)


def test_agent_pool_reuses_one_agent_and_closes_its_client():
    class FakeClient:
        def __init__(self):
            self.closed = 0

        async def close(self):
            self.closed += 1

    class FakePydanticAgent:
        def __init__(self):
            self.entered = 0
            self.exited = 0
            self.model = SimpleNamespace(client=FakeClient())

        async def __aenter__(self):
            self.entered += 1
            return self

        async def __aexit__(self, *_):
            self.exited += 1

    async def check():
        pool = AgentPool()
        resolved = ResolvedProvider(base_url="http://localhost:11434/v1", api_key="test-key", model="test-model")
        agent = FakePydanticAgent()

        assert await pool.get("chat", resolved, lambda: agent) is agent
        assert await pool.get("chat", resolved, lambda: FakePydanticAgent()) is agent
        await pool.close()

        assert (agent.entered, agent.exited, agent.model.client.closed) == (1, 1, 1)

    asyncio.run(check())


def test_behavior_planning_document_is_loaded_for_agent(tmp_path: Path):
    guide = tmp_path / "mirdo_behavior_planning.md"
    guide.write_text("动作完成后等待 Godot 回调。", encoding="utf-8")

    assert load_behavior_guide(tmp_path) == "动作完成后等待 Godot 回调。"


def test_personality_bible_is_loaded_as_stable_agent_instruction(tmp_path: Path):
    bible = tmp_path / "mirdo_personality_bible.md"
    bible.write_text("Mirdo 永远称呼玩家为老师。", encoding="utf-8")

    assert load_personality_bible(tmp_path) == "Mirdo 永远称呼玩家为老师。"


def test_chat_graph_parallelizes_context_and_joins_before_planning():
    diagram = CHAT_GRAPH.render()

    assert "context_retrieval" in diagram
    assert "join_context" in diagram
    assert "retrieve_memory" in diagram
    assert "retrieve_knowledge" in diagram


def test_prompt_marks_autonomous_request_source():
    prompt = PromptBuilder().build(
        request=ChatRequest(player_text="Mirdo 主动想一想", context={"request_source": "autonomous"})
    )

    assert "request_source=autonomous" in prompt


def test_prompt_includes_verified_godot_event_context():
    prompt = PromptBuilder().build(
        request=ChatRequest(
            player_text="到达后反馈",
            context={
                "request_source": "autonomous",
                "event_context": {
                    "event_id": "event:task-toilet:navigation_goal_finished",
                    "event": "navigation_goal_finished",
                    "ok": True,
                    "task_id": "task-toilet",
                    "target_name": "卫生间",
                    "intent_report": {"target_marker_path": "Main/ToiletLook", "chosen_action": "curious_peek"},
                    "action_result": {"arrived": True},
                    "runtime_snapshot": {
                        "perception": {"nearby_objects": [{"id": "sink", "name": "水槽"}]},
                        "current_behavior": {"current_kind": "go_to_nav_point"},
                    },
                },
            },
        )
    )

    assert "<godot_event>" in prompt
    assert "event_id=event:task-toilet:navigation_goal_finished" in prompt
    assert "target_marker_path" in prompt
    assert "runtime_snapshot.current_behavior" in prompt
    assert "runtime_snapshot.perception=" in prompt

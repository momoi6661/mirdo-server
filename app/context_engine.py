"""Mirdo 的上下文策略与运行时上下文组装。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .schemas import ChatRequest


@dataclass(frozen=True)
class ContextPlan:
    """本回合上下文预算；由协议状态生成，不由模型自由修改。"""

    mode: str = "chat"
    max_recent_messages: int = 6
    max_memory_hits: int = 0
    max_knowledge_hits: int = 0
    max_story_hits: int = 0
    include_world_state: bool = False
    include_verified_task: bool = True


class MirdoContextEngine:
    """上下文管家：控制安全边界和预算，不负责生成回复。"""

    def __init__(self, settings: Any | None = None) -> None:
        """接收可选设置；不传设置时使用适合单元测试的默认预算。"""
        self.settings = settings
    def build(
        self,
        *,
        request: ChatRequest,
        memory_facts: list[Any] | None = None,
        knowledge_hits: list[Any] | None = None,
        story_events: list[dict[str, Any]] | None = None,
        session_summary: str = "",
        context_plan: ContextPlan | None = None,
    ) -> str:
        """构造本回合的运行时 instructions，不把对话原文塞进 instructions。

        对话历史通过 PydanticAI ``message_history`` 传递；这里仅放当前世界状态和已经检索
        的长期上下文。
        """
        plan = context_plan or self.plan(request)
        runtime = self._runtime_state(request, plan)
        memories = self._format_memory_facts((memory_facts or [])[: plan.max_memory_hits])
        knowledge = self._format_knowledge((knowledge_hits or [])[: plan.max_knowledge_hits])
        stories = self._format_story_events((story_events or [])[: plan.max_story_hits])
        # 空的 XML 区块也会占用输入 token；只发送本回合真正需要的资料。
        # 这不会改变人格固定前缀，只压缩动态后缀，利于上游 Prompt Cache 命中。
        sections = [f"<runtime_state>\n{runtime}\n</runtime_state>"]
        if memories:
            sections.append(f"<long_term_memory>\n{memories}\n</long_term_memory>")
        if stories:
            sections.append(f"<shared_story_events>\n{stories}\n</shared_story_events>")
        # 普通闲聊不携带旧会话摘要；只有回忆、动作回执或自主任务才需要它。
        include_summary = plan.mode != "chat" or plan.max_memory_hits > 0 or plan.max_story_hits > 0
        if include_summary and session_summary.strip():
            sections.append(f"<session_summary>\n{session_summary.strip()[:2400]}\n</session_summary>")
        if knowledge:
            sections.append(f"<knowledge_candidates>\n{knowledge}\n</knowledge_candidates>")
        context = "\n\n".join(sections)
        # 防止异常感知或故事文档把一次普通请求膨胀成超长 prompt。
        max_chars = 9000 if plan.include_world_state else 4200
        return context[:max_chars]

    def plan(self, request: ChatRequest) -> ContextPlan:
        """根据请求来源生成计划；不调用模型，避免增加一次模型延迟。"""
        context = request.context if isinstance(request.context, dict) else {}
        source = str(context.get("request_source", "player") or "player").strip().lower()
        has_task = bool(context.get("task_chain") or context.get("source_decision") or context.get("verified_task"))
        recent_limit = int(getattr(self.settings, "chat_context_recent_messages", 6))
        memory_limit = int(getattr(self.settings, "chat_context_memory_hits", 3))
        knowledge_limit = int(getattr(self.settings, "chat_context_knowledge_hits", 2))
        story_limit = int(getattr(self.settings, "chat_context_story_hits", 2))
        if source in {"godot_tool", "godot_tool_result", "godot_runtime", "autonomous", "autonomous_idle", "autonomous_task"} or has_task:
            return ContextPlan(
                mode="action_receipt" if source in {"godot_tool", "godot_tool_result"} else "action",
                max_recent_messages=4,
                max_memory_hits=2,
                # 自主对话也必须知道共同经历，否则 Mirdo 会像刚认识老师一样重新开话题。
                max_story_hits=story_limit if source in {"godot_tool", "godot_tool_result", "autonomous_idle", "autonomous_task"} else 0,
                include_world_state=True,
            )

        # 普通闲聊不预取知识；明确回忆/背景问题时才注入少量候选。
        text = str(request.player_text or "")
        recall_intent = any(marker in text for marker in ("记得", "以前", "上次", "故事", "背景", "发生过"))
        return ContextPlan(
            mode="chat",
            max_recent_messages=recent_limit,
            max_memory_hits=memory_limit if recall_intent else 0,
            max_knowledge_hits=knowledge_limit if recall_intent else 0,
            max_story_hits=story_limit if recall_intent else 0,
        )

    def _runtime_state(self, request: ChatRequest, plan: ContextPlan) -> str:
        stats = request.npc_stats
        context = request.context if isinstance(request.context, dict) else {}
        if plan.include_world_state:
            world_fields = [
                f"npc={self._format_npc_contract(context.get('npc'))}",
                f"perception={self._format_relevant_perception(context)}",
                f"navigation_catalog={self._format_relevant_nav_points(context)}",
                f"outing_return={self._format_outing_return(context)}",
                f"source_decision={self._format_source_decision(context)}",
                f"task_chain={self._format_task_chain(context)}",
                f"verified_task={self._format_verified_task(context.get('verified_task'))}",
                f"<godot_event>\n{self._format_event_context(context.get('event_context'))}\n</godot_event>",
                f"world_scene={self._format_world_scene(context.get('world_scene'))}",
            ]
        else:
            # 闲聊只保留身份和任务摘要，不展开全量感知、导航与场景对象。
            world_fields = [
                f"npc={self._format_npc_contract(context.get('npc'))}",
                f"task_chain={self._format_task_chain(context) if plan.include_verified_task else '(none)'}",
            ]
        return "\n".join(
            [
                f"context_mode={plan.mode}",
                f"session_id={request.session_id}",
                f"request_source={context.get('request_source', 'player')}",
                f"steering={self._format_steering(request.steering)}",
                f"day={request.day}",
                f"time_min={request.effective_time_min()}",
                f"given_item={request.given_item}",
                f"use_tts={request.use_tts}",
                f"tts_voice_profile={request.tts_voice_profile}",
                f"generate_japanese={request.generate_japanese}",
                f"npc_stats.hunger={stats.hunger}",
                f"npc_stats.thirst={stats.thirst}",
                f"npc_stats.mood={stats.mood}",
                f"npc_stats.favor={stats.favor}",
                *world_fields,
            ]
        )

    def _target_refs(self, context: dict[str, Any]) -> set[str]:
        """收集任务协议中的目标 ID；不通过模型猜测对象。"""
        refs: set[str] = set()
        for block_name in ("source_decision", "event_context", "verified_task", "task_chain"):
            block = context.get(block_name, {})
            if not isinstance(block, dict):
                continue
            for key in ("target_object", "target_nav_point", "target_ref", "target_name", "item_id", "last_target"):
                value = block.get(key)
                if isinstance(value, str) and value.strip():
                    refs.add(value.strip().lower())
        return refs

    def _format_relevant_perception(self, context: dict[str, Any]) -> str:
        """动作回合只展开目标对象；没有明确目标时最多保留附近前六项。"""
        perception = context.get("perception", {})
        if not isinstance(perception, dict):
            return "(none)"
        refs = self._target_refs(context)
        selected: list[dict[str, Any]] = []
        for section in ("nearby_objects", "visible_items"):
            entries = perception.get(section, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                identity = " ".join(str(entry.get(k, "")) for k in ("id", "name", "target_ref")).lower()
                if not refs or any(ref in identity for ref in refs):
                    selected.append(entry)
        if not selected and refs:
            # 目标可能尚未出现在感知快照中，保留少量候选帮助 Agent 解释失败原因。
            selected = [entry for entry in perception.get("nearby_objects", [])[:3] if isinstance(entry, dict)]
        return self._format_perception({"nearby_objects": selected[:6]})

    def _format_relevant_nav_points(self, context: dict[str, Any]) -> str:
        """动作回合只展开目标导航点，避免把整张地图发给模型。"""
        refs = self._target_refs(context)
        entries = context.get("navigation_catalog", context.get("known_nav_points", []))
        if not isinstance(entries, list):
            return "(none)"
        if refs:
            entries = [
                entry for entry in entries
                if isinstance(entry, dict)
                and any(ref in " ".join(str(entry.get(k, "")) for k in ("id", "name", "target_ref")).lower() for ref in refs)
            ]
        return self._format_nav_points({"navigation_catalog": entries[:6]})

    def _format_steering(self, steering: Any) -> str:
        """把实时引导的协议字段放入运行时状态，而不是伪装成历史对白。"""
        mode = str(getattr(steering, "mode", "none") or "none")
        if mode == "none":
            return "(none)"
        fields = {
            "mode": mode,
            "phase": getattr(steering, "phase", "idle"),
            "target_request_id": getattr(steering, "target_request_id", ""),
            "target_client_sequence": getattr(steering, "target_client_sequence", 0),
            "interrupted_dialogue": getattr(steering, "interrupted_dialogue", ""),
            "heard_dialogue": getattr(steering, "heard_dialogue", ""),
            "boundary_reason": getattr(steering, "boundary_reason", ""),
            "reason": getattr(steering, "reason", ""),
        }
        return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))

    def _format_verified_task(self, task: Any) -> str:
        """格式化由 Godot 确认的动作结果；它比模型自己的 task_status 更可信。"""
        if not isinstance(task, dict) or not task:
            return "(none)"
        keys = ["task_id", "goal", "command", "target_ref", "status", "last_event", "last_result"]
        return " ".join(f"{key}={task.get(key, '')}" for key in keys if key in task) or "(none)"

    def _format_event_context(self, event_context: Any) -> str:
        """格式化 Godot 动作完成快照，帮助 Agent 先解释事实再规划后果。"""
        if not isinstance(event_context, dict) or not event_context:
            return "(none)"
        lines: list[str] = []
        scalar_keys = [
            "event_id", "tool_call_id", "event", "status", "ok", "reason", "task_id", "chain_id", "chain_depth",
            "current_step_id", "step_id", "command", "target_ref", "target_object", "target_nav_point", "target_name", "target_description",
            "marker_role", "arrival_action",
        ]
        for key in scalar_keys:
            if key not in event_context:
                continue
            value = event_context[key]
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, default=str)
            lines.append(f"{key}={str(value)[:600]}")
        for key in ["action_step", "action_line", "intent", "intent_report", "action_result", "execution", "observation"]:
            value = event_context.get(key)
            if isinstance(value, dict) and value:
                encoded = json.dumps(value, ensure_ascii=False, default=str)
                lines.append(f"{key}={encoded[:1200]}")
        runtime_snapshot = event_context.get("runtime_snapshot")
        if isinstance(runtime_snapshot, dict) and runtime_snapshot:
            for key in ["current_behavior", "mind_state", "resource_stats"]:
                value = runtime_snapshot.get(key)
                if isinstance(value, dict) and value:
                    encoded = json.dumps(value, ensure_ascii=False, default=str)
                    lines.append(f"runtime_snapshot.{key}={encoded[:1200]}")
            perception = runtime_snapshot.get("perception")
            if isinstance(perception, dict) and perception:
                lines.append(f"runtime_snapshot.perception={self._format_perception(perception)}")
        return "\n".join(lines) or "(none)"

    def _format_task_chain(self, context: Any) -> str:
        if not isinstance(context, dict):
            return "(none)"
        chain = context.get("task_chain", context.get("ai_task_chain", {}))
        if not isinstance(chain, dict) or not chain:
            decision = context.get("source_decision", {})
            if isinstance(decision, dict) and str(decision.get("chain_id", "") or "").strip():
                return "chain_id={chain_id} chain_depth={chain_depth}".format(
                    chain_id=decision.get("chain_id", ""),
                    chain_depth=decision.get("chain_depth", ""),
                )
            return "(none)"
        keys = ["chain_id", "chain_depth", "status", "goal", "last_result", "last_target", "visited_targets", "pending_question", "should_continue"]
        parts: list[str] = []
        for key in keys:
            if key not in chain:
                continue
            value = chain.get(key, "")
            if isinstance(value, list):
                value = ",".join(str(item) for item in value[-8:])
            parts.append(f"{key}={value}")
        return " ".join(parts) if parts else "(none)"

    def _format_source_decision(self, context: Any) -> str:
        if not isinstance(context, dict):
            return "(none)"
        decision = context.get("source_decision", {})
        if not isinstance(decision, dict) or not decision:
            return "(none)"
        keys = ["kind", "event", "tool_call_id", "task_id", "ok", "current_step_id", "step_id", "command", "target_ref", "target_nav_point", "target_object", "target_name", "target_description", "action_hint", "arrival_action", "marker_role", "last_dialogue", "next_decision_hint", "reason", "chain_depth", "chain_id"]
        return " ".join(f"{key}={decision.get(key, '')}" for key in keys if key in decision)

    def _format_outing_return(self, context: Any) -> str:
        if not isinstance(context, dict):
            return "(none)"
        payload = context.get("real_outing_return", context.get("outing_return", context))
        if not isinstance(payload, dict):
            return "(none)"
        event = str(payload.get("event", context.get("event", "")) or "").strip()
        real = bool(payload.get("real_outing", context.get("real_outing", False)))
        if event != "real_outing_return" and not real:
            return "(none)"
        keys = ["event", "real_outing", "location_id", "location_name", "total_minutes", "route_minutes", "search_minutes", "loot_added", "loot_lost", "carried_count", "returned_count", "consumed_count", "health_damage", "risk"]
        return " ".join(f"{key}={payload.get(key, context.get(key, ''))}" for key in keys if key in payload or key in context)

    def _format_npc_contract(self, npc: Any) -> str:
        if not isinstance(npc, dict):
            return "(default:Mirdo)"
        name = npc.get("name", "")
        actions = npc.get("available_body_actions", [])
        expressions = npc.get("available_expressions", [])
        personality = npc.get("personality_knowledge", "")
        contract = npc.get("response_contract", "")
        return "name={name} actions={actions} expressions={expressions} personality={personality} contract={contract}".format(
            name=name,
            actions=",".join(str(v) for v in actions) if isinstance(actions, list) else actions,
            expressions=",".join(str(v) for v in expressions) if isinstance(expressions, list) else expressions,
            personality=str(personality)[:500],
            contract=str(contract)[:500],
        )

    def _format_perception(self, perception: Any) -> str:
        if not isinstance(perception, dict):
            return "(none)"
        lines: list[str] = []
        for section in ("nearby_objects", "areas", "visible_items"):
            entries = perception.get(section, [])
            if not isinstance(entries, list) or not entries:
                continue
            lines.append(f"{section}:")
            for entry in entries[:12]:
                if not isinstance(entry, dict):
                    continue
                object_id = entry.get("id", "")
                name = entry.get("name", "")
                object_type = entry.get("type", "")
                description = entry.get("description", "")
                tags = entry.get("tags", [])
                actions = entry.get("actions", [])
                marker_roles = entry.get("marker_roles", {})
                distance = entry.get("distance", "")
                lines.append(
                    "- id={id} name={name} type={type} distance={distance} tags={tags} actions={actions} marker_roles={marker_roles} desc={desc}".format(
                        id=object_id,
                        name=name,
                        type=object_type,
                        distance=distance,
                        tags=",".join(str(v) for v in tags) if isinstance(tags, list) else tags,
                        actions=",".join(str(v) for v in actions) if isinstance(actions, list) else actions,
                        marker_roles=",".join(str(k) for k in marker_roles.keys()) if isinstance(marker_roles, dict) else marker_roles,
                        desc=description,
                    )
                )
        return "\n".join(lines) if lines else "(none)"

    def _format_nav_points(self, context: Any) -> str:
        if not isinstance(context, dict):
            return "(none)"
        entries = context.get("navigation_catalog", context.get("known_nav_points", context.get("ai_nav_points", [])))
        if not isinstance(entries, list) or not entries:
            perception = context.get("perception", {})
            if isinstance(perception, dict):
                entries = perception.get("known_nav_points", [])
        if not isinstance(entries, list) or not entries:
            return "(none)"
        lines = [
            "Semantic navigation catalog; entities expose capabilities, while Godot keeps coordinates and Marker paths private:",
        ]
        for entry in entries[:24]:
            if not isinstance(entry, dict):
                continue
            lines.append(
                "- id={id} target_ref={target_ref} kind={kind} name={name} type={type} distance={distance} tags={tags} affordances={affordances} availability={availability} hint={hint} desc={desc}".format(
                    id=entry.get("id", ""),
                    target_ref=entry.get("target_ref", entry.get("entity_id", entry.get("id", ""))),
                    kind=entry.get("kind", "waypoint"),
                    name=entry.get("name", ""),
                    type=entry.get("type", ""),
                    distance=entry.get("distance", ""),
                    tags=",".join(str(v) for v in entry.get("tags", [])) if isinstance(entry.get("tags", []), list) else entry.get("tags", ""),
                    affordances=",".join(str(v) for v in entry.get("affordances", entry.get("action_options", []))) if isinstance(entry.get("affordances", entry.get("action_options", [])), list) else entry.get("affordances", entry.get("action_options", "")),
                    availability=json.dumps(entry.get("availability", {}), ensure_ascii=False, default=str),
                    hint=entry.get("action_hint", ""),
                    desc=entry.get("description", ""),
                )
            )
        return "\n".join(lines) if len(lines) > 1 else "(none)"


    def _format_world_scene(self, world_scene: Any) -> str:
        if not isinstance(world_scene, dict):
            return "(none)"
        lines: list[str] = []
        scene_name = str(world_scene.get("scene_name", "") or "").strip()
        if scene_name:
            lines.append(f"scene_name={scene_name}")
        for section in ("world_objects", "world_areas"):
            entries = world_scene.get(section, [])
            if not isinstance(entries, list) or not entries:
                continue
            lines.append(f"{section}:")
            for entry in entries[:24]:
                if not isinstance(entry, dict):
                    continue
                tags = entry.get("tags", [])
                actions = entry.get("affordances", entry.get("actions", entry.get("supported_actions", [])))
                availability = entry.get("availability", {})
                lines.append(
                    "- id={id} name={name} kind={kind} type={type} distance={distance} tags={tags} affordances={actions} availability={availability} desc={desc}".format(
                        id=entry.get("id", ""),
                        name=entry.get("name", ""),
                        kind=entry.get("kind", "entity"),
                        type=entry.get("type", ""),
                        distance=entry.get("distance", ""),
                        tags=",".join(str(v) for v in tags) if isinstance(tags, list) else tags,
                        actions=",".join(str(v) for v in actions) if isinstance(actions, list) else actions,
                        availability=json.dumps(availability, ensure_ascii=False, default=str),
                        desc=entry.get("description", ""),
                    )
                )
        return "\n".join(lines) if lines else "(none)"

    def _format_memory_facts(self, facts: list[Any]) -> str:
        """把检索到的长期事实限制为小块运行时上下文。"""
        lines = [
            "- {subject} {predicate}: {value}".format(
                subject=self._get_value(fact, "subject", "player"),
                predicate=self._get_value(fact, "predicate", "fact"),
                value=self._get_value(fact, "value", ""),
            )
            for fact in facts[:6]
            if str(self._get_value(fact, "value", "")).strip()
        ]
        return "\n".join(lines) or "（无）"

    def _format_story_events(self, events: list[dict[str, Any]]) -> str:
        """把最近共同经历作为事实背景，而不是伪造的聊天消息。"""
        lines = [f"- {str(event.get('summary', ''))[:300]}" for event in events[:4] if str(event.get("summary", "")).strip()]
        return "\n".join(lines) or "（无）"

    def _format_knowledge(self, hits: list[Any]) -> str:
        """压缩 RAG 命中的知识候选，深查仍交给 search_knowledge tool。"""
        lines = [
            "- [{source}] {text}".format(
                source=self._get_value(hit, "source", "knowledge"),
                text=str(self._get_value(hit, "text", ""))[:450],
            )
            for hit in hits[:3]
            if str(self._get_value(hit, "text", "")).strip()
        ]
        return "\n".join(lines) or "（无）"

    def _get_value(self, item: Any, key: str, default: Any) -> Any:
        """兼容字典和对象属性的读取。

        这是 Python 的鸭子类型：历史记录不必继承同一基类，只要能提供所需字段即可。
        """
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)


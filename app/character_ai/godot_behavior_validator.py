"""Godot 行为输出的最后一道轻量安全校验。

复杂语义（为什么做、做完以后做什么）属于 PydanticAI + 行为规划文档；这里绝不再维护第二套 AI。
"""
from __future__ import annotations

from ..dialogue_text import effective_player_intent_text
from ..schemas import ActionStep, ChatRequest, ChatResponse


DEFAULT_ACTIONS = {"idle_normal", "idle_alert", "listen", "walk", "run", "tiny_wave", "react_nod", "curious_peek", "work_count_supplies", "work_check_shelf", "work_inspect_cabinet", "work_check_lower"}
DEFAULT_EXPRESSIONS = {"neutral", "joy", "fun", "angry", "sorrow", "surprised"}
DEFAULT_VISEMES = {"aa", "ih", "ou", "E", "oh"}
COMMANDS = {"", "go_to_marker", "go_to_object", "go_to_nav_point", "sit_down", "follow_player", "stop_follow", "look_at_player", "pick_up_item", "take_from_container", "use_item", "eat_item", "give_item_to_player"}


class GodotBehaviorValidator:
    """校验动作线的首步，不在这里替 Agent 改写后续剧情。"""

    def finalize_response(self, request: ChatRequest, response: ChatResponse) -> ChatResponse:
        """校验 Agent 已规划的动作线首步，不重新决定剧情或行为意图。

        输入是 Agent 产出的 ChatResponse；返回仍是同一个对象，方便 ChatOrchestrator 继续持久化。
        """
        actions = self._contract_list(request, "available_body_actions", DEFAULT_ACTIONS)
        expressions = self._contract_list(request, "available_expressions", DEFAULT_EXPRESSIONS)
        visemes = self._contract_list(request, "available_visemes", DEFAULT_VISEMES)

        response.dialogue = self._clean_dialogue(response.dialogue)
        response.action = response.action if response.action in actions else self._first(actions, "listen")
        response.expression = response.expression if response.expression in expressions else self._emotion_expression(response.emotion, expressions)
        response.visemes = self._clean_visemes(response.visemes, visemes)
        response.viseme_sequence = self._clean_visemes(response.viseme_sequence, visemes)
        self._normalize_action_line(response)

        # 外出归来只允许关心和对话，不让模型抢走 Godot 的结算流程。
        if self._is_outing_return(request):
            self._clear_action_line(response)
            if response.action in {"walk", "run"}:
                response.action = self._first(actions, "tiny_wave")
            return response

        current = self._current_step(response)
        if current is None:
            return response
        if current.command not in COMMANDS or not self._validate_target(request, current):
            # 首步不可执行时整条线作废，避免 Godot 跳过首步偷偷执行后续步骤。
            self._clear_action_line(response)
            return response
        self._inherit_chain_context(request, current)
        response.current_step_id = current.step_id
        return response

    def local_fallback_response(self, request: ChatRequest) -> ChatResponse:
        """模型不可用时只给安全、短小、不会虚构事实的回应。"""
        text = effective_player_intent_text(request).lower()
        if any(word in text for word in ("别跟", "停止跟", "不要跟", "stop follow")):
            return ChatResponse(dialogue="嗯，我在这里等老师。", expression="neutral", action="idle_normal", action_line=[ActionStep(step_id="stop-follow", command="stop_follow", reason="老师要求停止跟随")], fallback=True, error="model_call_failed")
        if any(word in text for word in ("跟着我", "跟上", "follow me")):
            return ChatResponse(dialogue="好呀老师，我跟着你。", expression="joy", action="walk", action_line=[ActionStep(step_id="follow-player", command="follow_player", command_payload={"follow_target": "player"}, reason="老师要求跟随")], fallback=True, error="model_call_failed")
        return ChatResponse(dialogue="老师，我在。需要我先看哪里吗？", expression="neutral", action="listen", fallback=True, error="model_call_failed")

    def _normalize_action_line(self, response: ChatResponse) -> None:
        """补齐稳定 step_id，并把 current_step_id 限定为动作线中的 pending 步骤。"""
        normalized: list[ActionStep] = []
        for index, raw_step in enumerate(response.action_line[:4], start=1):
            step = raw_step if isinstance(raw_step, ActionStep) else ActionStep.model_validate(raw_step)
            if not step.step_id:
                step.step_id = f"step_{index}"
            step.status = step.status or "pending"
            normalized.append(step)
        response.action_line = normalized
        if not normalized:
            response.current_step_id = ""
            return
        selected = next((step for step in normalized if step.step_id == response.current_step_id and step.status not in {"done", "complete", "cancelled"}), None)
        if selected is None:
            selected = next((step for step in normalized if step.status not in {"done", "complete", "cancelled"}), normalized[0])
        response.current_step_id = selected.step_id

    @staticmethod
    def _current_step(response: ChatResponse) -> ActionStep | None:
        """读取本回合唯一允许执行的首个动作步骤。"""
        if not response.current_step_id:
            return None
        return next((step for step in response.action_line if step.step_id == response.current_step_id), None)

    @staticmethod
    def _clear_action_line(response: ChatResponse) -> None:
        """清空不可执行动作线，宁可只对白也不把虚构目标交给 Godot。"""
        response.action_line = []
        response.current_step_id = ""

    def _validate_target(self, request: ChatRequest, step: ActionStep) -> bool:
        """目标必须来自 Godot 的本轮感知或导航点，不能让模型凭空造地点。"""
        payload = step.command_payload
        if step.command in {"go_to_marker", "go_to_object", "sit_down"}:
            valid = self._object_ids(request)
            target = str(payload.get("target_object", payload.get("target_ref", "")))
            if target in valid:
                return True
            # go_to_marker 也允许指向语义导航目录；Godot 会在本地解析
            # Marker/NodePath，不能把这个旧命令误判成无效动作。
            return step.command == "go_to_marker" and target in self._nav_ids(request)
        if step.command == "go_to_nav_point":
            valid = self._nav_ids(request)
            return str(payload.get("target_nav_point", payload.get("target_ref", ""))) in valid
        if step.command in {"pick_up_item", "use_item", "eat_item"}:
            valid = self._pickable_ids(request)
            return str(payload.get("target_object", payload.get("target_ref", ""))) in valid
        if step.command == "take_from_container":
            target = str(payload.get("target_object", payload.get("target_ref", "")))
            item_id = str(payload.get("item_id", payload.get("item", ""))).strip()
            return target in self._object_ids(request) and bool(item_id)
        if step.command == "give_item_to_player":
            return bool(str(payload.get("item_id", payload.get("item", ""))).strip())
        return step.command in {"follow_player", "stop_follow", "look_at_player", ""}

    @staticmethod
    def _inherit_chain_context(request: ChatRequest, step: ActionStep) -> None:
        """把 Godot 回调的链标识带到首步，保证下一次回调仍能关联同一条动作线。"""
        context = request.context if isinstance(request.context, dict) else {}
        source = context.get("source_decision", {}) if isinstance(context.get("source_decision", {}), dict) else {}
        payload = dict(step.command_payload)
        for key in ("chain_id", "chain_depth"):
            if key in source and key not in payload:
                payload[key] = source[key]
        step.command_payload = payload

    def _contract_list(self, request: ChatRequest, key: str, default: set[str]) -> set[str]:
        context = request.context if isinstance(request.context, dict) else {}
        npc = context.get("npc", {}) if isinstance(context.get("npc", {}), dict) else {}
        values = npc.get(key, [])
        return {str(value) for value in values} if isinstance(values, list) and values else set(default)

    def _object_ids(self, request: ChatRequest) -> set[str]:
        context = request.context if isinstance(request.context, dict) else {}
        perception = context.get("perception", {}) if isinstance(context.get("perception", {}), dict) else {}
        valid = {
            str(item.get("id"))
            for group in ("nearby_objects", "visible_items", "areas")
            for item in perception.get(group, [])
            if isinstance(item, dict) and item.get("id")
        }
        world_scene = context.get("world_scene", {})
        if isinstance(world_scene, dict):
            valid.update(
                str(item.get("id"))
                for item in world_scene.get("world_objects", [])
                if isinstance(item, dict) and item.get("id")
            )
        valid.update(self._navigation_catalog_ids(request, entity_only=True))
        return valid

    def _nav_ids(self, request: ChatRequest) -> set[str]:
        context = request.context if isinstance(request.context, dict) else {}
        points = context.get("navigation_catalog", context.get("known_nav_points", context.get("ai_nav_points", [])))
        return {str(point.get("id")) for point in points if isinstance(point, dict) and point.get("id")} if isinstance(points, list) else set()

    def _navigation_catalog_ids(self, request: ChatRequest, *, entity_only: bool = False) -> set[str]:
        """读取语义导航目录；目录中的 Marker/坐标从不进入校验层。"""
        context = request.context if isinstance(request.context, dict) else {}
        values = context.get("navigation_catalog", context.get("known_nav_points", context.get("ai_nav_points", [])))
        if not isinstance(values, list):
            return set()
        result: set[str] = set()
        for item in values:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if entity_only and str(item.get("kind", "")).strip().lower() not in {"entity", "storage", "table", "seat", "bed", "facility", "generic", "food", "tool", "weapon", "medical", "door", "exit"}:
                continue
            result.add(str(item["id"]))
        return result

    def _pickable_ids(self, request: ChatRequest) -> set[str]:
        """只允许 Godot 当前感知到、明确带 pickable 能力的物品。"""
        context = request.context if isinstance(request.context, dict) else {}
        perception = context.get("perception", {}) if isinstance(context.get("perception", {}), dict) else {}
        valid: set[str] = set()
        for group in ("nearby_objects", "visible_items"):
            values = perception.get(group, [])
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                tags = {str(tag).lower() for tag in item.get("tags", [])} if isinstance(item.get("tags", []), list) else set()
                actions = {str(action).lower() for action in item.get("actions", [])} if isinstance(item.get("actions", []), list) else set()
                if "pickable" in tags or "pick_up" in actions:
                    valid.add(str(item["id"]))
        return valid

    @staticmethod
    def _clean_dialogue(value: str) -> str:
        """清理旧称呼并保证 Godot 至少能显示一句文本。"""
        text = str(value or "").strip().replace("小空", "Mirdo").replace("队长", "老师").replace("主人", "老师").replace("指挥官", "老师").replace("玩家", "老师")
        return text or "老师，我在。"

    @staticmethod
    def _clean_visemes(value: str, allowed: set[str]) -> str:
        """过滤掉 Godot 当前没有对应口型资源的 token。"""
        return "、".join(token for token in str(value or "").replace(",", "、").split("、") if token.strip() in allowed)

    @staticmethod
    def _first(values: set[str], fallback: str) -> str:
        """从 Godot 允许集合中选一个稳定的默认值。"""
        return fallback if fallback in values else sorted(values)[0]

    @staticmethod
    def _emotion_expression(emotion: str, allowed: set[str]) -> str:
        """仅在 Agent 未提供合法表情时，用情绪挑选保守替代。"""
        text = str(emotion or "")
        wanted = "sorrow" if any(word in text for word in ("担心", "疲惫", "害怕", "困")) else "joy" if any(word in text for word in ("开心", "温和", "乖巧")) else "neutral"
        return wanted if wanted in allowed else GodotBehaviorValidator._first(allowed, "neutral")

    @staticmethod
    def _is_outing_return(request: ChatRequest) -> bool:
        """识别由 Godot 外出系统结算的返程事件。"""
        context = request.context if isinstance(request.context, dict) else {}
        return bool(context.get("real_outing")) and context.get("event") == "real_outing_return"

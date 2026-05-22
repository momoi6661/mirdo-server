from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..schemas import ChatRequest, ChatResponse


DEFAULT_ACTIONS = {
    "idle_normal",
    "idle_relaxed",
    "idle_sleepy",
    "idle_alert",
    "idle_fidget",
    "listen",
    "happy_bounce",
    "walk",
    "run",
    "seated_idle",
    "seated_sleepy",
    "work_inspect_cabinet",
    "work_check_shelf",
    "work_check_lower",
    "work_count_supplies",
    "work_reach",
    "work_take_item",
    "work_place_item",
    "work_drink",
    "work_explain",
    "react_nod",
    "react_wave",
    "tiny_wave",
    "rub_eye",
    "sleepy_yawn",
    "cute_startle",
    "curious_peek",
    "tilt_head_cute",
    "look_back",
    "look_around",
    "turn_left",
    "turn_right",
    "turn_180",
}
DEFAULT_EXPRESSIONS = {"neutral", "joy", "fun", "angry", "sorrow", "surprised"}
DEFAULT_VISEMES = ["aa", "ih", "ou", "E", "oh"]


@dataclass(frozen=True)
class ObjectRule:
    canonical_id: str
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    action: str
    expression: str = "neutral"


OBJECT_RULES: tuple[ObjectRule, ...] = (
    ObjectRule(
        "food_cabinet",
        ("食物柜", "食品柜", "补给柜", "吃的", "罐头", "饮水", "水柜", "food", "supply"),
        ("food", "supplies", "water", "storage"),
        "work_count_supplies",
        "surprised",
    ),
    ObjectRule(
        "medical_cabinet",
        ("医疗柜", "医药柜", "药柜", "药品", "急救", "medical", "medicine"),
        ("medical", "medicine", "first_aid"),
        "work_check_shelf",
        "neutral",
    ),
    ObjectRule(
        "equipment_cabinet",
        ("武器柜", "装备柜", "武器", "装备", "工具柜", "equipment", "weapon"),
        ("equipment", "weapon"),
        "work_inspect_cabinet",
        "neutral",
    ),
    ObjectRule(
        "utility_storage_box",
        ("杂物箱", "物资箱", "工具箱", "储物箱", "材料箱", "utility", "box"),
        ("utility", "tool", "material"),
        "work_check_lower",
        "neutral",
    ),
    ObjectRule(
        "dining_table",
        ("桌子", "餐桌", "桌面", "table"),
        ("table", "social"),
        "look_around",
        "neutral",
    ),
)


class CharacterBehaviorPlanner:
    """Deterministic post-processor for game actions.

    The LLM writes dialogue/personality. This planner keeps executable fields
    stable for Godot: valid action names, valid expressions, object commands,
    and simple fallback intents when the model is absent or vague.
    """

    def finalize_response(self, request: ChatRequest, response: ChatResponse) -> ChatResponse:
        actions = self._available_actions(request)
        expressions = self._available_expressions(request)
        visemes = self._available_visemes(request)
        text = request.player_text.strip()

        status_answer = self._status_answer(request)
        if status_answer is not None:
            dialogue, expression, action = status_answer
            response.dialogue = dialogue
            response.expression = expression
            response.action = action
            response.command = ""
            response.command_payload = {}

        self._sanitize_dialogue(response, request)
        response.expression = self._normalize_expression(response.expression, response.emotion, expressions)
        response.action = self._normalize_action(response.action, actions)
        response.visemes = self._normalize_visemes(response.visemes, visemes)
        response.viseme_sequence = self._normalize_visemes(response.viseme_sequence, visemes)
        if not response.visemes and not response.viseme_sequence:
            response.visemes = self._rough_visemes(response.dialogue, visemes)

        if self._is_real_outing_return(request):
            response.command = ""
            response.command_payload = {}
            response.action = self._normalize_action(response.action, actions)
            if response.action in {"walk", "run"}:
                response.action = self._first_available(actions, ("tiny_wave", "react_wave", "happy_bounce", "listen", "idle_normal"))
            if not response.action or response.action == "idle_normal":
                response.action = self._first_available(actions, ("tiny_wave", "react_wave", "happy_bounce", "listen", "idle_normal"))
            return response

        planned = self._plan_from_player_text(request, response)
        if planned:
            command, payload, action, expression = planned
            if not response.command:
                response.command = command
            if not response.command_payload:
                response.command_payload = payload
            if action:
                response.action = self._normalize_action(action, actions)
            if expression:
                response.expression = self._normalize_expression(expression, response.emotion, expressions)
        elif response.command == "go_to_object":
            self._repair_go_to_payload(request, response)

        response.command = self._normalize_command(response.command)
        if response.command == "go_to_nav_point":
            self._repair_go_to_nav_point_payload(request, response)
            if not response.command_payload.get("target_nav_point"):
                response.command = ""
                response.command_payload = {}
        if response.command == "go_to_object" and not response.command_payload.get("target_object"):
            response.command = ""
            response.command_payload = {}
        if response.command in {"follow_player", "stop_follow", "look_at_player"}:
            response.command_payload = response.command_payload if isinstance(response.command_payload, dict) else {}
        return response

    def local_fallback_response(self, request: ChatRequest) -> ChatResponse | None:
        status_answer = self._status_answer(request)
        if status_answer is not None:
            dialogue, expression, action = status_answer
            return ChatResponse(
                dialogue=dialogue,
                emotion="关心",
                expression=expression,
                action=action,
                command="",
                command_payload={},
                visemes="aa、ih、ou",
                memory_tags=["local_status_fallback"],
                session_id=request.session_id,
                fallback=True,
                error="model_call_failed",
            )

        lower = request.player_text.lower()
        if self._contains_any(lower, ("跟着我", "跟上", "follow me", "come with me")):
            return ChatResponse(
                dialogue="嗯，我跟着老师走。你慢一点的话，我也会努力跟上的。",
                emotion="乖巧",
                expression="joy",
                action="walk",
                command="follow_player",
                command_payload={"follow_target": "player"},
                visemes="aa、ih、ou",
                memory_tags=["local_behavior_fallback"],
                session_id=request.session_id,
                fallback=True,
                error="model_call_failed",
            )
        if self._contains_any(lower, ("别跟", "停下", "不用跟", "stop follow", "stay")):
            return ChatResponse(
                dialogue="好，我先停在这里等老师。",
                emotion="温和",
                expression="neutral",
                action="idle_normal",
                command="stop_follow",
                command_payload={},
                visemes="aa、ih、ou",
                memory_tags=["local_behavior_fallback"],
                session_id=request.session_id,
                fallback=True,
                error="model_call_failed",
            )
        dummy = ChatResponse(
            dialogue="好呀老师，我去看一下。",
            emotion="认真",
            expression="neutral",
            action="listen",
            session_id=request.session_id,
            fallback=True,
            error="model_call_failed",
        )
        planned = self._plan_from_player_text(request, dummy)
        if not planned:
            return None
        command, payload, action, expression = planned
        dummy.command = command
        dummy.command_payload = payload
        dummy.action = action
        dummy.expression = expression
        dummy.visemes = "aa、ih、ou"
        dummy.memory_tags = ["local_behavior_fallback"]
        return dummy

    def _status_answer(self, request: ChatRequest) -> tuple[str, str, str] | None:
        lower = request.player_text.lower().strip()
        asks_hunger = self._contains_any(lower, ("饿不饿", "饿吗", "你饿", "肚子饿", "hungry"))
        asks_thirst = self._contains_any(lower, ("渴不渴", "渴吗", "你渴", "口渴", "thirsty"))
        asks_tired = self._contains_any(lower, ("累不累", "累吗", "你累", "困不困", "困吗", "疲惫", "tired", "sleepy"))
        asks_general = self._contains_any(lower, ("状态怎么样", "感觉怎么样", "身体怎么样", "还好吗", "难受吗"))
        if not (asks_hunger or asks_thirst or asks_tired or asks_general):
            return None

        hunger = self._stat_value(request, "hunger", 65.0)
        thirst = self._stat_value(request, "thirst", 60.0)
        energy = self._stat_value(request, "energy", 70.0)
        mood = self._stat_value(request, "mood", 55.0)

        if asks_hunger:
            if hunger <= 25.0:
                return "老师，Mirdo 有点饿了……不过还可以陪你，等会儿想看看食物柜。", "sorrow", "listen"
            if hunger <= 50.0:
                return "老师，有一点点饿，不过还不严重哦，我还能陪着你。", "neutral", "listen"
            return "老师，我现在不太饿哦，先不用担心我。", "joy", "listen"

        if asks_thirst:
            if thirst <= 25.0:
                return "老师，Mirdo 有点渴了……如果方便的话，等会儿想补一点水。", "sorrow", "listen"
            if thirst <= 50.0:
                return "老师，有一点点渴，但还可以忍住，我会注意的。", "neutral", "listen"
            return "老师，我现在不太渴哦，谢谢老师关心。", "joy", "listen"

        if asks_tired:
            if energy <= 35.0:
                return "老师，Mirdo 有点累了……但还想待在老师身边。", "sorrow", "rub_eye"
            if energy <= 55.0:
                return "老师，有一点困困的，不过还可以继续陪你。", "neutral", "listen"
            return "老师，我现在不累哦，精神还可以。", "joy", "listen"

        if hunger <= 25.0:
            return "老师，我有点饿，状态不算太好……想先确认一下食物补给。", "sorrow", "listen"
        if thirst <= 25.0:
            return "老师，我有点渴，想先确认一下饮水。", "sorrow", "listen"
        if energy <= 35.0:
            return "老师，我有点累，不过还能慢慢陪你。", "sorrow", "rub_eye"
        if mood <= 35.0:
            return "老师，我还好，只是有点没精神……你在的话会安心一点。", "sorrow", "listen"
        return "老师，我现在状态还不错哦，可以继续陪你守着避难所。", "joy", "listen"

    def _stat_value(self, request: ChatRequest, key: str, default: float) -> float:
        context = request.context if isinstance(request.context, dict) else {}
        resource_stats = context.get("resource_stats", {})
        if isinstance(resource_stats, dict) and key in resource_stats:
            try:
                return float(resource_stats.get(key, default))
            except (TypeError, ValueError):
                return default
        try:
            return float(getattr(request.npc_stats, key))
        except (TypeError, ValueError, AttributeError):
            return default

    def _plan_from_player_text(self, request: ChatRequest, response: ChatResponse) -> tuple[str, dict[str, Any], str, str] | None:
        text = request.player_text.strip()
        lower = text.lower()
        if self._contains_any(lower, ("别跟", "停下", "不用跟", "stop follow", "stay")):
            return "stop_follow", {}, "idle_normal", "neutral"
        if self._contains_any(lower, ("跟着我", "跟上", "follow me", "come with me")):
            return "follow_player", {"follow_target": "player"}, "walk", "joy"
        if self._contains_any(lower, ("看着我", "看我", "听我", "look at me")):
            return "look_at_player", {}, "listen", "neutral"

        wants_object = self._contains_any(
            lower,
            ("去", "看看", "查看", "检查", "打开", "拿", "取", "数", "整理", "look", "check", "inspect", "open", "take"),
        )
        if not wants_object:
            return None
        rule = self._match_object_rule(text, request)
        if rule is None:
            return None
        target_object = self._resolve_target_object(rule, request)
        if target_object and self._perception_has_object(request, target_object):
            marker_role = "open" if self._contains_any(lower, ("打开", "open")) else "approach"
            action = "work_take_item" if self._contains_any(lower, ("拿", "取", "take")) else rule.action
            return "go_to_object", {"target_object": target_object, "marker_role": marker_role}, action, rule.expression
        nav_point = self._resolve_target_nav_point(rule, request)
        if nav_point:
            action = "work_take_item" if self._contains_any(lower, ("拿", "取", "take")) else rule.action
            return "go_to_nav_point", {"target_nav_point": nav_point}, action, self._expression_for_nav_point(nav_point, request, rule.expression)
        if not target_object:
            return None
        marker_role = "open" if self._contains_any(lower, ("打开", "open")) else "approach"
        action = "work_take_item" if self._contains_any(lower, ("拿", "取", "take")) else rule.action
        return "go_to_object", {"target_object": target_object, "marker_role": marker_role}, action, rule.expression

    def _repair_go_to_payload(self, request: ChatRequest, response: ChatResponse) -> None:
        payload = response.command_payload if isinstance(response.command_payload, dict) else {}
        target = str(payload.get("target_object", payload.get("target_ref", ""))).strip()
        if target and self._perception_has_object(request, target):
            response.command_payload = {"target_object": target, "marker_role": str(payload.get("marker_role", "approach") or "approach")}
            return
        rule = self._match_object_rule(request.player_text, request)
        if rule is None:
            response.command_payload = payload
            return
        resolved = self._resolve_target_object(rule, request)
        if resolved:
            response.command_payload = {"target_object": resolved, "marker_role": str(payload.get("marker_role", "approach") or "approach")}

    def _repair_go_to_nav_point_payload(self, request: ChatRequest, response: ChatResponse) -> None:
        payload = response.command_payload if isinstance(response.command_payload, dict) else {}
        target = str(payload.get("target_nav_point", payload.get("nav_point", payload.get("point_id", "")))).strip()
        if target and self._nav_point_exists(request, target):
            response.command_payload = {"target_nav_point": target}
            return
        rule = self._match_object_rule(request.player_text, request)
        if rule is None:
            response.command_payload = payload
            return
        resolved = self._resolve_target_nav_point(rule, request)
        if resolved:
            response.command_payload = {"target_nav_point": resolved}

    def _match_object_rule(self, text: str, request: ChatRequest) -> ObjectRule | None:
        lower = text.lower()
        for rule in OBJECT_RULES:
            if self._contains_any(lower, rule.aliases):
                return rule
        perception_entries = self._perception_entries(request)
        for entry in perception_entries:
            haystack = " ".join(
                [
                    str(entry.get("id", "")),
                    str(entry.get("name", "")),
                    str(entry.get("type", "")),
                    str(entry.get("description", "")),
                    " ".join(str(tag) for tag in entry.get("tags", []) if isinstance(entry.get("tags", []), list)),
                ]
            ).lower()
            for rule in OBJECT_RULES:
                if any(alias.lower() in haystack and alias.lower() in lower for alias in rule.aliases):
                    return rule
                if any(tag.lower() in haystack and tag.lower() in lower for tag in rule.tags):
                    return rule
        return None

    def _resolve_target_object(self, rule: ObjectRule, request: ChatRequest) -> str:
        entries = self._perception_entries(request)
        scored: list[tuple[int, str]] = []
        for entry in entries:
            object_id = str(entry.get("id", "")).strip()
            if not object_id:
                continue
            haystack = " ".join(
                [
                    object_id,
                    str(entry.get("name", "")),
                    str(entry.get("type", "")),
                    str(entry.get("description", "")),
                    " ".join(str(tag) for tag in entry.get("tags", []) if isinstance(entry.get("tags", []), list)),
                ]
            ).lower()
            score = 0
            if object_id == rule.canonical_id:
                score += 20
            score += sum(6 for alias in rule.aliases if alias.lower() in haystack)
            score += sum(4 for tag in rule.tags if tag.lower() in haystack)
            if score > 0:
                scored.append((score, object_id))
        if scored:
            scored.sort(reverse=True)
            return scored[0][1]
        return rule.canonical_id

    def _resolve_target_nav_point(self, rule: ObjectRule, request: ChatRequest) -> str:
        scored: list[tuple[int, str]] = []
        for entry in self._nav_point_entries(request):
            point_id = str(entry.get("id", "")).strip()
            if not point_id:
                continue
            haystack = self._entry_haystack(entry)
            score = 0
            score += sum(8 for alias in rule.aliases if alias.lower() in haystack)
            score += sum(5 for tag in rule.tags if tag.lower() in haystack)
            actions = entry.get("action_options", [])
            if isinstance(actions, list) and rule.action in [str(action) for action in actions]:
                score += 5
            if point_id.startswith(rule.canonical_id):
                score += 4
            if score > 0:
                scored.append((score, point_id))
        if scored:
            scored.sort(reverse=True)
            return scored[0][1]
        return ""

    def _nav_point_exists(self, request: ChatRequest, target: str) -> bool:
        return any(str(entry.get("id", "")).strip() == target for entry in self._nav_point_entries(request))

    def _expression_for_nav_point(self, point_id: str, request: ChatRequest, fallback: str) -> str:
        for entry in self._nav_point_entries(request):
            if str(entry.get("id", "")).strip() != point_id:
                continue
            expressions = entry.get("expression_options", [])
            if isinstance(expressions, list) and expressions:
                return str(expressions[0]).strip() or fallback
            expression = str(entry.get("arrival_expression", "")).strip()
            return expression or fallback
        return fallback

    def _perception_has_object(self, request: ChatRequest, target: str) -> bool:
        return any(str(entry.get("id", "")).strip() == target for entry in self._perception_entries(request))

    def _perception_entries(self, request: ChatRequest) -> list[dict[str, Any]]:
        context = request.context if isinstance(request.context, dict) else {}
        perception = context.get("perception", {})
        if not isinstance(perception, dict):
            return []
        entries: list[dict[str, Any]] = []
        for section in ("nearby_objects", "visible_items", "areas"):
            raw_entries = perception.get(section, [])
            if isinstance(raw_entries, list):
                entries.extend(entry for entry in raw_entries if isinstance(entry, dict))
        return entries

    def _nav_point_entries(self, request: ChatRequest) -> list[dict[str, Any]]:
        context = request.context if isinstance(request.context, dict) else {}
        entries = context.get("known_nav_points", context.get("ai_nav_points", []))
        if not isinstance(entries, list):
            entries = []
        result = [entry for entry in entries if isinstance(entry, dict)]
        perception = context.get("perception", {})
        if isinstance(perception, dict):
            nested = perception.get("known_nav_points", [])
            if isinstance(nested, list):
                known_ids = {str(entry.get("id", "")).strip() for entry in result}
                for entry in nested:
                    if isinstance(entry, dict) and str(entry.get("id", "")).strip() not in known_ids:
                        result.append(entry)
        return result

    def _entry_haystack(self, entry: dict[str, Any]) -> str:
        return " ".join(
            [
                str(entry.get("id", "")),
                str(entry.get("name", "")),
                str(entry.get("type", "")),
                str(entry.get("description", "")),
                str(entry.get("action_hint", "")),
                str(entry.get("target_object_id", "")),
                " ".join(str(tag) for tag in entry.get("tags", []) if isinstance(entry.get("tags", []), list)),
            ]
        ).lower()

    def _available_actions(self, request: ChatRequest) -> set[str]:
        npc = self._npc_contract(request)
        values = npc.get("available_body_actions", [])
        if isinstance(values, list) and values:
            return {str(value).strip() for value in values if str(value).strip()}
        return set(DEFAULT_ACTIONS)

    def _available_expressions(self, request: ChatRequest) -> set[str]:
        npc = self._npc_contract(request)
        values = npc.get("available_expressions", [])
        if isinstance(values, list) and values:
            return {str(value).strip() for value in values if str(value).strip()}
        return set(DEFAULT_EXPRESSIONS)

    def _available_visemes(self, request: ChatRequest) -> list[str]:
        npc = self._npc_contract(request)
        values = npc.get("available_visemes", [])
        if isinstance(values, list):
            clean = [str(value).strip() for value in values if str(value).strip()]
            if clean:
                return clean
        return list(DEFAULT_VISEMES)

    def _npc_contract(self, request: ChatRequest) -> dict[str, Any]:
        context = request.context if isinstance(request.context, dict) else {}
        npc = context.get("npc", {})
        return npc if isinstance(npc, dict) else {}

    def _normalize_action(self, action: str, actions: set[str]) -> str:
        aliases = {"Idle": "idle_normal", "Talk": "listen", "talk": "listen", "idle": "idle_normal"}
        clean = str(action or "").strip()
        clean = aliases.get(clean, clean)
        if clean in actions:
            return clean
        for fallback in ("listen", "idle_normal", "tiny_wave"):
            if fallback in actions:
                return fallback
        return sorted(actions)[0] if actions else "idle_normal"

    def _normalize_expression(self, expression: str, emotion: str, expressions: set[str]) -> str:
        clean = str(expression or "").strip().lower()
        emotion_lower = str(emotion or "").lower()
        if clean not in expressions:
            if self._contains_any(emotion_lower, ("开心", "高兴", "温和", "乖巧", "happy", "joy")):
                clean = "joy"
            elif self._contains_any(emotion_lower, ("好奇", "困惑", "惊讶", "surprised")):
                clean = "surprised"
            elif self._contains_any(emotion_lower, ("担心", "疲惫", "难过", "害怕", "sorrow")):
                clean = "sorrow"
            elif self._contains_any(emotion_lower, ("调皮", "fun")):
                clean = "fun"
            elif self._contains_any(emotion_lower, ("生气", "angry")):
                clean = "angry"
            else:
                clean = "neutral"
        if clean in expressions:
            return clean
        return "neutral" if "neutral" in expressions else sorted(expressions)[0]

    def _normalize_visemes(self, value: str, allowed: list[str]) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        parts = [part.strip() for part in re.split(r"[、,，\s]+", raw) if part.strip()]
        allowed_set = set(allowed)
        clean = [part for part in parts if part in allowed_set]
        return "、".join(clean[:12])

    def _rough_visemes(self, dialogue: str, allowed: list[str]) -> str:
        if not dialogue.strip():
            return ""
        preferred = ["aa", "ih", "ou", "E", "oh"]
        sequence = [token for token in preferred if token in allowed]
        if not sequence:
            sequence = allowed[:]
        count = min(8, max(3, len(dialogue.strip()) // 4))
        return "、".join(sequence[index % len(sequence)] for index in range(count))

    def _is_real_outing_return(self, request: ChatRequest) -> bool:
        context = request.context if isinstance(request.context, dict) else {}
        event = str(context.get("event", context.get("context_event", "")) or "").strip()
        if event == "real_outing_return" and bool(context.get("real_outing", True)):
            return True
        payload = context.get("real_outing_return", context.get("outing_return", {}))
        return isinstance(payload, dict) and bool(payload.get("real_outing", False))

    def _first_available(self, actions: set[str], preferred: tuple[str, ...]) -> str:
        for action in preferred:
            if action in actions:
                return action
        return sorted(actions)[0] if actions else "idle_normal"

    def _normalize_command(self, command: str) -> str:
        clean = str(command or "").strip()
        allowed = {"", "go_to_object", "go_to_nav_point", "follow_player", "stop_follow", "look_at_player"}
        return clean if clean in allowed else ""

    def _sanitize_dialogue(self, response: ChatResponse, request: ChatRequest) -> None:
        dialogue = response.dialogue.strip()
        dialogue = dialogue.replace("队长", "老师")
        if "老师" not in dialogue and len(dialogue) <= 40:
            npc_name = str(self._npc_contract(request).get("name", "Mirdo") or "Mirdo").strip()
            if npc_name and npc_name not in dialogue:
                dialogue = f"老师，{dialogue}"
        response.dialogue = dialogue

    def _contains_any(self, text: str, needles: tuple[str, ...]) -> bool:
        lower = text.lower()
        return any(str(needle).lower() in lower for needle in needles)

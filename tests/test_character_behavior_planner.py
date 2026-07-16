from app.character_ai.godot_behavior_validator import GodotBehaviorValidator
from app.schemas import ActionStep, ChatRequest, ChatResponse


def _request() -> ChatRequest:
    return ChatRequest(
        player_text="检查食物柜",
        context={
            "npc": {"available_body_actions": ["listen", "work_count_supplies"], "available_expressions": ["neutral", "joy"]},
            "perception": {"nearby_objects": [{"id": "food_cabinet_runtime"}]},
            "known_nav_points": [{"id": "food_cabinet_approach"}],
        },
    )


def test_validator_keeps_agent_planned_valid_target_without_replanning():
    result = GodotBehaviorValidator().finalize_response(
        _request(),
        ChatResponse(dialogue="好呀老师，我去确认一下。", action="work_count_supplies", action_line=[ActionStep(step_id="inspect", command="go_to_object", command_payload={"target_object": "food_cabinet_runtime"})]),
    )
    assert result.current_step_id == "inspect"
    assert result.action_line[0].command == "go_to_object"
    assert result.task_status == ""
    assert result.task_reason == ""
    assert result.next_decision_hint == ""


def test_validator_drops_model_invented_target():
    result = GodotBehaviorValidator().finalize_response(
        _request(),
        ChatResponse(dialogue="我去地下仓库。", action_line=[ActionStep(step_id="invented", command="go_to_object", command_payload={"target_object": "invented_basement"})]),
    )
    assert result.action_line == []
    assert result.current_step_id == ""


def test_validator_does_not_replan_completed_goal():
    request = _request()
    request.context["source_decision"] = {"kind": "external_goal_follow_up", "target_object": "food_cabinet_runtime"}
    result = GodotBehaviorValidator().finalize_response(
        request,
        ChatResponse(dialogue="我再看一次。", action_line=[ActionStep(step_id="inspect-again", command="go_to_object", command_payload={"target_object": "food_cabinet_runtime"})]),
    )
    assert result.action_line[0].command == "go_to_object"


def test_validator_keeps_pickup_command_for_visible_pickable_item():
    request = _request()
    request.context["perception"]["visible_items"] = [{"id": "bandage", "tags": ["pickable"], "actions": ["pick_up"]}]
    result = GodotBehaviorValidator().finalize_response(
        request,
        ChatResponse(dialogue="老师，我拿起来看看。", action_line=[ActionStep(step_id="pickup", command="pick_up_item", command_payload={"target_object": "bandage"})]),
    )
    assert result.action_line[0].command == "pick_up_item"
    assert result.action_line[0].command_payload["target_object"] == "bandage"


def test_validator_accepts_take_from_real_container_then_give():
    request = _request()
    take = GodotBehaviorValidator().finalize_response(
        request,
        ChatResponse(dialogue="我到食品柜拿一瓶水。", action_line=[ActionStep(step_id="take", command="take_from_container", command_payload={"target_object": "food_cabinet_runtime", "item_id": "water_bottle"})]),
    )
    assert take.action_line[0].command == "take_from_container"
    give = GodotBehaviorValidator().finalize_response(
        request,
        ChatResponse(dialogue="老师，给你。", action_line=[ActionStep(step_id="give", command="give_item_to_player", command_payload={"item_id": "water_bottle"})]),
    )
    assert give.action_line[0].command == "give_item_to_player"


def test_fallback_only_handles_safe_follow_control():
    request = _request()
    request.player_text = "跟着我"
    result = GodotBehaviorValidator().local_fallback_response(request)
    assert result.action_line[0].command == "follow_player"


def test_validator_removes_legacy_identity_terms():
    result = GodotBehaviorValidator().finalize_response(_request(), ChatResponse(dialogue="小空收到队长的命令。"))
    assert "小空" not in result.dialogue
    assert "队长" not in result.dialogue


def test_validator_exposes_the_first_action_line_step_to_godot():
    """动作线的首步是当前唯一可执行步骤，后续步骤只作为计划返回。"""
    result = GodotBehaviorValidator().finalize_response(
        _request(),
        ChatResponse(
            dialogue="好呀老师，我先去确认食品柜。",
            action_line=[
                ActionStep(
                    step_id="inspect-cabinet",
                    command="go_to_object",
                    command_payload={"target_object": "food_cabinet_runtime"},
                    reason="先到柜子前确认有没有水和食物",
                    expected_result="到达后观察柜内物品",
                ),
                ActionStep(
                    step_id="look-for-water",
                    command="use_item",
                    command_payload={"target_object": "water_bottle"},
                    reason="只有看见可拿的水才能喝",
                ),
            ],
        ),
    )
    assert result.current_step_id == "inspect-cabinet"
    assert result.action_line[0].step_id == "inspect-cabinet"
    assert result.action_line[0].command == "go_to_object"


def test_validator_drops_only_invalid_current_action_line_step():
    """当前步目标不在 Godot 感知中时，不能把虚构目标交给执行器。"""
    result = GodotBehaviorValidator().finalize_response(
        _request(),
        ChatResponse(
            dialogue="我去地下仓库找水。",
            action_line=[
                ActionStep(
                    step_id="invented-place",
                    command="go_to_object",
                    command_payload={"target_object": "invented_basement"},
                ),
                ActionStep(step_id="say-more", command="", reason="到达后再说明情况"),
            ],
        ),
    )
    assert result.current_step_id == ""
    assert result.action_line == []


def test_validator_carries_godot_chain_identity_into_current_step():
    request = _request()
    request.context["source_decision"] = {"chain_id": "cabinet-line", "chain_depth": 2}
    result = GodotBehaviorValidator().finalize_response(
        request,
        ChatResponse(
            dialogue="我继续确认柜子。",
            action_line=[ActionStep(step_id="continue-check", command="go_to_object", command_payload={"target_object": "food_cabinet_runtime"})],
        ),
    )
    assert result.action_line[0].command_payload["chain_id"] == "cabinet-line"
    assert result.action_line[0].command_payload["chain_depth"] == 2

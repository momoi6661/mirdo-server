from app.prompt_builder import PromptBuilder
from app.schemas import ChatRequest


def test_prompt_builder_includes_runtime_state_and_recent_turns():
    builder = PromptBuilder()
    request = ChatRequest(player_text="你好", day=2, time_min=600, npc_stats={"mood": 3}, given_item="水")

    messages = builder.build(
        request=request,
        recent_turns=[{"role": "assistant", "content": "早上好。"}],
        memory_facts=[{"subject": "player", "predicate": "likes", "value": "罐头汤"}],
        knowledge_hits=[],
    )

    flattened = "\n".join(content for _role, content in messages)
    assert "只输出 JSON" in flattened
    assert "称为老师" in flattened
    assert "称为队长" not in flattened
    assert "day=2" in flattened
    assert "time_min=600" in flattened
    assert "given_item=水" in flattened
    assert "早上好" in flattened
    assert "罐头汤" in flattened
    assert messages[-1] == ("user", "你好")


def test_prompt_builder_uses_mirdo_npc_contract():
    builder = PromptBuilder()
    request = ChatRequest(
        player_text="去看看食物柜",
        context={
            "npc": {
                "name": "Mirdo",
                "role_prompt": "可爱的避难所少女 NPC，会称呼玩家为老师。",
                "available_body_actions": ["idle_normal", "listen", "happy_bounce", "work_count_supplies"],
                "available_expressions": ["neutral", "joy", "surprised"],
                "available_visemes": ["aa", "ih", "ou"],
            },
            "perception": {
                "nearby_objects": [
                    {
                        "id": "food_cabinet",
                        "name": "食品柜",
                        "type": "storage",
                        "description": "存放食物和饮水补给的柜子。",
                        "tags": ["storage", "food", "supplies"],
                        "actions": ["go_to", "inspect", "open"],
                        "marker_roles": {"approach": "/a", "open": "/b"},
                    }
                ]
            },
            "known_nav_points": [
                {
                    "id": "food_cabinet_1_approach",
                    "name": "食品柜1号检查点",
                    "type": "supplies",
                    "description": "食品柜前方站位。",
                    "tags": ["storage", "food", "supplies"],
                    "action_options": ["work_count_supplies"],
                    "expression_options": ["neutral"],
                }
            ],
        },
    )

    messages = builder.build(request=request, knowledge_hits=[])
    flattened = "\n".join(content for _role, content in messages)
    assert "Mirdo" in flattened
    assert "happy_bounce" in flattened
    assert "work_count_supplies" in flattened
    assert "surprised" in flattened
    assert "food_cabinet" in flattened
    assert "food_cabinet_1_approach" in flattened
    assert "go_to_nav_point" in flattened
    assert "Area3D" in flattened
    assert "go_to_object" in flattened
    assert "perception 里的 object id" in flattened
    assert "5 类元音口型" in flattened
    assert "aa、ih、ou" in flattened
    assert "viseme_sequence" in flattened


def test_prompt_builder_includes_real_outing_return_rule():
    builder = PromptBuilder()
    request = ChatRequest(
        player_text="我回来了",
        context={
            "event": "real_outing_return",
            "real_outing": True,
            "location_name": "超市",
            "total_minutes": 86,
            "loot_added": 3,
            "health_damage": 4,
            "npc": {
                "name": "Mirdo",
                "available_body_actions": ["tiny_wave", "happy_bounce", "listen"],
                "available_expressions": ["neutral", "joy", "sorrow"],
            },
        },
    )

    messages = builder.build(request=request)
    flattened = "\n".join(content for _role, content in messages)

    assert "real_outing_return" in flattened
    assert "不要输出 go_to_object" in flattened
    assert "location_name=超市" in flattened
    assert "health_damage=4" in flattened


def test_prompt_builder_includes_task_chain_context_and_recent_command_payload():
    builder = PromptBuilder()
    request = ChatRequest(
        session_id="s1",
        player_text="到镜子以后继续判断下一步",
        context={
            "source_decision": {
                "kind": "external_goal_follow_up",
                "chain_id": "mirror_chain",
                "chain_depth": 2,
                "target_nav_point": "bathroom_mirror_look",
                "target_name": "卫生间镜子",
            },
            "task_chain": {
                "chain_id": "mirror_chain",
                "chain_depth": 2,
                "goal": "检查卫生间镜子",
                "last_target": "bathroom_mirror_look",
                "visited_targets": ["bathroom_mirror_look"],
            },
        },
    )

    messages = builder.build(
        request=request,
        recent_turns=[
            {"role": "user", "content": "去厕所看看镜子里面有什么。"},
            {
                "role": "assistant",
                "content": "好呀老师，我去看看镜子。",
                "payload": {
                    "command": "go_to_nav_point",
                    "command_payload": {
                        "target_nav_point": "bathroom_mirror_look",
                        "chain_id": "mirror_chain",
                        "chain_depth": 1,
                    },
                },
            },
        ],
    )

    flattened = "\n".join(content for _role, content in messages)
    assert "task_chain=chain_id=mirror_chain" in flattened
    assert "goal=检查卫生间镜子" in flattened
    assert "source_decision=kind=external_goal_follow_up" in flattened
    assert "assistant: 好呀老师，我去看看镜子。" in flattened
    assert "chain_id=mirror_chain depth=1 command=go_to_nav_point target=bathroom_mirror_look" in flattened
    assert "是否继续、结束或换目标由你判断" in flattened
